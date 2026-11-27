"""Isolated Lia Voz pilot. No SIP, CRM or WhatsApp side effects.

PCM contract: mono signed 16-bit little endian, 24000 Hz.
All protocol and playback methods must run on the same asyncio event loop.
A future SIP adapter must marshal native media callbacks onto that loop.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlencode

PCM_RATE = 24000
PCM_BYTES_PER_SECOND = PCM_RATE * 2
MAX_INPUT_BYTES = PCM_BYTES_PER_SECOND // 5  # 200 ms
MAX_BUFFER_BYTES = PCM_BYTES_PER_SECOND * 15
MODEL = "gpt-realtime-2.1-mini"
VOICE = "marin"

# Not imported from the WhatsApp SDR. No customer history is loaded here.
PILOT_INSTRUCTIONS = (
    "Voce e Lia Voz, assistente virtual comercial da Sulnet. "
    "Converse em portugues brasileiro, com frases curtas e naturais. "
    "Identifique-se como assistente virtual. Escute antes de responder. "
    "Este e um ambiente interno de homologacao: nao prometa contratacao, "
    "precos, cobertura, transferencia ou consulta ao CRM. Nao solicite "
    "CPF, senhas, dados financeiros ou outros dados pessoais. "
    "Respeite imediatamente o pedido de encerrar ou nao receber ligacoes."
)


class LiaVoiceError(RuntimeError):
    """Sanitized error; never include API keys, audio or transcript."""


def session_update(model: str = MODEL, voice: str = VOICE) -> dict:
    """GA Realtime session schema; no tools during the isolated pilot."""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": model,
            "output_modalities": ["audio"],
            "instructions": PILOT_INSTRUCTIONS,
            "tools": [],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": PCM_RATE},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 550,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": PCM_RATE},
                    "voice": voice,
                },
            },
        },
    }


def audio_append(pcm: bytes) -> dict:
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > MAX_INPUT_BYTES:
        raise ValueError("Expected 1-200 ms of complete PCM16 samples")
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }


@dataclass(frozen=True)
class AudioItem:
    response_id: str
    item_id: str
    content_index: int


class Playback:
    """Bounded output queue; distinguish generated audio from played samples."""

    def __init__(self) -> None:
        self.item: AudioItem | None = None
        self.pending = bytearray()
        self.played_bytes = 0

    def push(self, item: AudioItem, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            raise LiaVoiceError("Invalid PCM output")
        if item != self.item:
            if self.pending:
                raise LiaVoiceError("Overlapping output audio items")
            self.item = item
            self.played_bytes = 0
        if len(self.pending) + len(pcm) > MAX_BUFFER_BYTES:
            raise LiaVoiceError("Output audio buffer exceeded")
        self.pending.extend(pcm)

    def pull(self, size: int) -> bytes:
        """Return PCM with silence padding; count only generated samples consumed."""
        if size <= 0 or size % 2 or size > MAX_INPUT_BYTES:
            raise ValueError("Invalid playback frame size")
        n = min(size, len(self.pending))
        result = bytes(self.pending[:n])
        del self.pending[:n]
        self.played_bytes += n
        return result + bytes(size - n)

    def clear(self) -> None:
        self.pending.clear()
        self.item = None
        self.played_bytes = 0

    def interrupt(self) -> dict | None:
        item = self.item
        # Also truncate a partially streamed response whose queue is empty:
        # generation may still be in progress.
        event = None
        if item is not None:
            event = {
                "type": "conversation.item.truncate",
                "item_id": item.item_id,
                "content_index": item.content_index,
                "audio_end_ms": self.played_bytes * 1000 // PCM_BYTES_PER_SECOND,
            }
        self.clear()
        return event


class RealtimeProtocol:
    """Per-call state machine. Server VAD cancels generation; we clear playback."""

    def __init__(self) -> None:
        self.playback = Playback()
        self.active_response: str | None = None
        self.discarded_responses: set[str] = set()

    def handle(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind == "error":
            self.playback.clear()
            raise LiaVoiceError("Realtime reported an error")
        if kind == "response.created":
            self.active_response = event["response"]["id"]
        elif kind == "response.done":
            response = event["response"]
            if response.get("id") == self.active_response:
                self.active_response = None
            if response.get("status") in {"failed", "incomplete"}:
                self.playback.clear()
                raise LiaVoiceError("Realtime response did not complete")
        elif kind == "response.output_audio.delta":
            response_id = event["response_id"]
            if response_id in self.discarded_responses:
                return []
            try:
                pcm = base64.b64decode(event["delta"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise LiaVoiceError("Invalid audio encoding") from None
            self.playback.push(
                AudioItem(response_id, event["item_id"], event["content_index"]), pcm
            )
        elif kind == "input_audio_buffer.speech_started":
            if self.active_response:
                self.discarded_responses.add(self.active_response)
            if self.playback.item:
                self.discarded_responses.add(self.playback.item.response_id)
            # A completed and fully played item needs no truncation.
            if not self.playback.pending and self.active_response is None:
                self.playback.clear()
                return []
            truncate = self.playback.interrupt()
            return [truncate] if truncate else []
        return []


class RealtimeSession:
    """Async WebSocket adapter; use a single reader and one media producer."""

    def __init__(self, socket, model: str = MODEL, voice: str = VOICE) -> None:
        self._socket = socket
        self._model = model
        self._voice = voice
        self._send_lock = asyncio.Lock()
        self.protocol = RealtimeProtocol()
        self.ready = False

    async def _send(self, event: dict) -> None:
        async with self._send_lock:
            await self._socket.send(json.dumps(event))

    async def _receive(self) -> dict:
        event = json.loads(await self._socket.recv())
        if not isinstance(event, dict):
            raise LiaVoiceError("Invalid Realtime event")
        return event

    async def start(self) -> None:
        if self.ready:
            raise LiaVoiceError("Session already started")
        async with asyncio.timeout(15):
            await self._send(session_update(self._model, self._voice))
            while True:
                event = await self._receive()
                self.protocol.handle(event)
                if event.get("type") == "session.updated":
                    self.ready = True
                    return

    async def append_pcm(self, pcm: bytes) -> None:
        if not self.ready:
            raise LiaVoiceError("Session is not ready")
        await self._send(audio_append(pcm))

    async def greet(self) -> None:
        if not self.ready:
            raise LiaVoiceError("Session is not ready")
        await self._send({
            "type": "response.create",
            "response": {
                "instructions": (
                    "Apresente-se como Lia Voz, assistente virtual da Sulnet, "
                    "e pergunte se a pessoa pode conversar agora. Nao ofereca planos."
                )
            },
        })

    async def receive_once(self) -> str:
        if not self.ready:
            raise LiaVoiceError("Session is not ready")
        event = await self._receive()
        for outgoing in self.protocol.handle(event):
            await self._send(outgoing)
        return event.get("type", "")

    def pull_pcm(self, size: int) -> bytes:
        return self.protocol.playback.pull(size)


@asynccontextmanager
async def open_realtime(*, enabled: bool = False, api_key: str = "",
                        model: str = MODEL, voice: str = VOICE,
                        max_seconds: int = 180):
    """Explicit opt-in, bounded connection; never falls back to the chat agent.

    The caller must keep its media tasks within the context and close them
    before exiting. No automatic reconnect or redial.
    """
    if not enabled:
        raise LiaVoiceError("Lia Voz pilot disabled")
    if not api_key.strip():
        raise LiaVoiceError("Lia Voz API key not configured")
    if not 15 <= max_seconds <= 300:
        raise ValueError("Pilot duration must be 15-300 seconds")
    # Optional dependency, imported only for explicitly enabled internal tests.
    from websockets.asyncio.client import connect

    url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": model})
    async with asyncio.timeout(max_seconds):
        async with connect(
            url, additional_headers={"Authorization": "Bearer " + api_key},
            open_timeout=10, close_timeout=3, max_size=2 * 1024 * 1024,
            max_queue=16, ping_interval=20, ping_timeout=10,
        ) as socket:
            session = RealtimeSession(socket, model, voice)
            try:
                await session.start()
                yield session
            finally:
                session.ready = False
                session.protocol.playback.clear()
