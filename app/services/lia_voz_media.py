"""Isolated native-media adapter for a single authorized Lia Voz test.

No dialing, recording, persistence, SIP account changes or CRM operations.
Native audio callbacks do no network I/O and use bounded, per-call buffers.
The existing PJSUA2 worker does not import or activate this adapter.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from threading import Lock

from app.services.lia_voz_realtime import LiaVoiceError, PCM_RATE

FRAME_MS = 20
FRAME_BYTES = PCM_RATE * 2 * FRAME_MS // 1000
INPUT_CAPACITY = 25  # 500 ms; terminate test on backpressure instead of stale speech
SILENCE = bytes(FRAME_BYTES)


class MediaBridge:
    """One call's native-thread input queue and synchronized output consumer."""

    def __init__(self, session, *, input_capacity: int = INPUT_CAPACITY):
        if not session.ready:
            raise LiaVoiceError("Realtime session not ready")
        if not 1 <= input_capacity <= INPUT_CAPACITY:
            raise ValueError("Invalid input queue capacity")
        self._session = session
        self._input_capacity = input_capacity
        self._queue = deque()
        self._lock = Lock()
        self._closed = False
        self._failure = None
        self._received = 0
        self._rendered = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def snapshot(self) -> dict:
        """Counters only: no audio, PII, API key or conversation transcript."""
        with self._lock:
            return {
                "received_frames": self._received,
                "requested_frames": self._rendered,
                "queued_frames": len(self._queue),
                "closed": self._closed,
                "failed": self._failure is not None,
            }

    def _close_locked(self) -> None:
        self._closed = True
        self._queue.clear()

    def fail(self, reason: str) -> None:
        # First failure wins. Callers pass fixed, non-sensitive messages only.
        with self._lock:
            if not self._closed:
                self._failure = reason
                self._close_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def ingest(self, pcm: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES:
                self._failure = "Invalid native input frame"
                self._close_locked()
                return
            if len(self._queue) >= self._input_capacity:
                self._failure = "Native audio input overrun"
                self._close_locked()
                return
            self._queue.append(pcm)
            self._received += 1

    def next_input(self) -> bytes | None:
        with self._lock:
            return self._queue.popleft() if self._queue and not self._closed else None

    def render(self) -> bytes:
        # Lock order: media bridge -> RealtimeSession playback lock.
        # Realtime never calls back into this bridge while holding its lock.
        with self._lock:
            if self._closed:
                return SILENCE
            try:
                pcm = self._session.pull_pcm(FRAME_BYTES)
                if len(pcm) != FRAME_BYTES:
                    raise ValueError("Incorrect output size")
            except Exception:
                self._failure = "Native audio output failed"
                self._close_locked()
                return SILENCE
            self._rendered += 1
            return pcm


def create_pj_port(pj, bridge: MediaBridge):
    """Register a 24 kHz mono PCM16 port; PJSIP converts conference formats.

    Caller owns the port on the PJSUA2 thread and must disconnect it before
    destroying the port or endpoint. Compatible with 2.15.1 SWIG ByteVector.
    """
    class LiaAudioPort(pj.AudioMediaPort):
        def onFrameReceived(self, frame):
            try:
                if frame.type != pj.PJMEDIA_FRAME_TYPE_AUDIO:
                    return
                if frame.size != FRAME_BYTES or frame.buf.size() != FRAME_BYTES:
                    bridge.fail("Unexpected native input frame size")
                    return
                bridge.ingest(bytes(frame.buf[i] for i in range(FRAME_BYTES)))
            except Exception:
                bridge.fail("Native receive callback failed")

        def onFrameRequested(self, frame):
            try:
                if frame.size != FRAME_BYTES:
                    bridge.fail("Unexpected native output frame size")
                    frame.type = pj.PJMEDIA_FRAME_TYPE_NONE
                    frame.size = 0
                    return
                vector = pj.ByteVector()
                for byte in bridge.render():
                    vector.append(byte)
                frame.buf = vector
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.size = FRAME_BYTES
            except Exception:
                bridge.fail("Native render callback failed")
                frame.type = pj.PJMEDIA_FRAME_TYPE_NONE
                frame.size = 0

    fmt = pj.MediaFormatAudio()
    fmt.id = pj.PJMEDIA_FORMAT_PCM
    fmt.type = pj.PJMEDIA_TYPE_AUDIO
    fmt.clockRate = PCM_RATE
    fmt.channelCount = 1
    fmt.frameTimeUsec = FRAME_MS * 1000
    fmt.bitsPerSample = 16
    fmt.avgBps = PCM_RATE * 16
    fmt.maxBps = PCM_RATE * 16
    port = LiaAudioPort()
    port.createPort("lia-voz-pilot", fmt)
    return port


@contextmanager
def attach_pj_media(pj, call_audio, bridge: MediaBridge, *, enabled: bool = False):
    """Wire an already-established authorized test call; never originate one.

    Enter/exit on the PJSUA2 owner thread. Keep bridge and port alive until
    both directions are disconnected; destroy endpoint only afterwards.
    """
    if not enabled or bridge.closed:
        raise LiaVoiceError("Native Lia Voz pilot disabled or closed")
    port = create_pj_port(pj, bridge)
    inbound = outbound = False
    try:
        call_audio.startTransmit(port)
        inbound = True
        port.startTransmit(call_audio)
        outbound = True
        yield port
    finally:
        bridge.close()
        failed = False
        if outbound:
            try:
                port.stopTransmit(call_audio)
            except Exception:
                failed = True
        if inbound:
            try:
                call_audio.stopTransmit(port)
            except Exception:
                failed = True
        if failed:
            raise LiaVoiceError("Native audio disconnect failed")


async def run_media_bridge(session, bridge: MediaBridge, *, greet: bool = True):
    """Run within the already-open Realtime context until native close/failure.

    The native call owner must call bridge.close on hangup or admin abort.
    All socket tasks are cancelled/joined before returning. No redial/retry.
    """
    if not session.ready or bridge.closed:
        raise LiaVoiceError("Media bridge not ready")

    async def send_audio():
        while not bridge.closed:
            frame = bridge.next_input()
            if frame is None:
                await asyncio.sleep(0.005)
            else:
                await session.append_pcm(frame)

    async def receive_audio():
        while not bridge.closed:
            await session.receive_once()

    async def watch_close():
        while not bridge.closed:
            await asyncio.sleep(0.02)

    tasks = []
    try:
        if greet:
            await session.greet()
        tasks = [
            asyncio.create_task(send_audio()),
            asyncio.create_task(receive_audio()),
            asyncio.create_task(watch_close()),
        ]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if bridge.failure:
            raise LiaVoiceError(bridge.failure)
        if not bridge.closed:
            raise LiaVoiceError("Audio bridge stopped unexpectedly")
    except LiaVoiceError:
        raise
    except Exception:
        raise LiaVoiceError("Realtime media transport failed") from None
    finally:
        bridge.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session.clear_pcm()
