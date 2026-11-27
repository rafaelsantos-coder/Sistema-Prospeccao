"""Offline unit tests: no API key, network, SIP or external calls."""
import asyncio
import base64
import json
from collections import deque

import pytest

from app.services.lia_voz_realtime import (
    AudioItem, LiaVoiceError, MAX_BUFFER_BYTES, MODEL, Playback,
    RealtimeProtocol, RealtimeSession, audio_append, open_realtime, session_update,
)


def created(response_id="r1"):
    return {"type": "response.created", "response": {"id": response_id}}


def delta(response_id="r1", item_id="i1", pcm=bytes(960)):
    return {
        "type": "response.output_audio.delta", "response_id": response_id,
        "item_id": item_id, "content_index": 0,
        "delta": base64.b64encode(pcm).decode(),
    }


SPEECH = {"type": "input_audio_buffer.speech_started"}


def test_session_is_voice_only_and_no_tools_or_chat_context():
    config = session_update()["session"]
    assert config["model"] == MODEL
    assert config["output_modalities"] == ["audio"]
    assert config["tools"] == []
    assert "Lia Voz" in config["instructions"]
    assert config["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert config["audio"]["input"]["turn_detection"]["interrupt_response"] is True


@pytest.mark.parametrize("pcm", [b"", b"x", bytes(9602), "invalid"])
def test_invalid_pcm_is_rejected(pcm):
    with pytest.raises(ValueError):
        audio_append(pcm)


def test_audio_is_base64_encoded_without_mutation():
    pcm = b"\x01\x02" * 480
    assert base64.b64decode(audio_append(pcm)["audio"]) == pcm


def test_silence_padding_is_not_counted_as_playback():
    p = Playback()
    p.push(AudioItem("r", "i", 0), b"\x01\x02" * 240)
    assert p.pull(960) == b"\x01\x02" * 240 + bytes(480)
    assert p.interrupt()["audio_end_ms"] == 10


def test_buffer_is_bounded_and_rejects_overlapping_items():
    p = Playback()
    item = AudioItem("r", "i", 0)
    p.push(item, bytes(MAX_BUFFER_BYTES))
    with pytest.raises(LiaVoiceError):
        p.push(item, bytes(2))
    with pytest.raises(LiaVoiceError):
        p.push(AudioItem("r2", "i2", 0), bytes(2))


def test_interruption_cuts_pending_audio_and_drops_late_deltas():
    protocol = RealtimeProtocol()
    protocol.handle(created())
    protocol.handle(delta(pcm=b"\x11\x22" * 960))
    assert protocol.playback.pull(960) == b"\x11\x22" * 480
    assert protocol.handle(SPEECH) == [{
        "type": "conversation.item.truncate", "item_id": "i1",
        "content_index": 0, "audio_end_ms": 20,
    }]
    assert protocol.playback.pull(960) == bytes(960)
    protocol.handle(delta(pcm=b"\x33\x44" * 480))
    assert protocol.playback.pull(960) == bytes(960)
    protocol.handle(created("r2"))
    protocol.handle(delta("r2", "i2", b"\x55\x66" * 480))
    assert protocol.playback.pull(960) == b"\x55\x66" * 480


def test_interruption_before_first_delta_drops_stale_response():
    p = RealtimeProtocol()
    p.handle(created())
    assert p.handle(SPEECH) == []
    p.handle(delta(pcm=b"\x11\x22" * 480))
    assert p.playback.pull(960) == bytes(960)


def test_completed_response_is_truncated_if_playback_remains():
    p = RealtimeProtocol()
    p.handle(created())
    p.handle(delta())
    p.handle({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
    assert p.handle(SPEECH)[0]["audio_end_ms"] == 0


def test_fully_played_completed_response_needs_no_truncate():
    p = RealtimeProtocol()
    p.handle(created())
    p.handle(delta())
    p.playback.pull(960)
    p.handle({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
    assert p.handle(SPEECH) == []


def test_invalid_output_and_remote_error_fail_closed():
    p = RealtimeProtocol()
    event = delta()
    event["delta"] = "not base64!"
    with pytest.raises(LiaVoiceError):
        p.handle(event)
    p.handle(delta())
    with pytest.raises(LiaVoiceError, match="Realtime reported an error"):
        p.handle({"type": "error", "error": {"message": "private data"}})
    assert not p.playback.pending


def test_independent_calls_do_not_share_audio_or_context():
    a, b = RealtimeProtocol(), RealtimeProtocol()
    a.handle(created())
    a.handle(delta(pcm=b"\x11\x22" * 480))
    assert b.playback.pull(960) == bytes(960)
    a.handle(SPEECH)
    assert not b.discarded_responses


class FakeSocket:
    def __init__(self, events):
        self.events = deque(events)
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(self.events.popleft())


def test_socket_handshake_greeting_audio_and_barge_in():
    async def run():
        socket = FakeSocket([
            {"type": "session.created"}, {"type": "session.updated"},
            created(), delta(pcm=bytes(1920)), SPEECH,
        ])
        session = RealtimeSession(socket)
        with pytest.raises(LiaVoiceError):
            await session.greet()
        await session.start()
        await session.greet()
        await session.append_pcm(bytes(960))
        await session.receive_once()
        await session.receive_once()
        session.pull_pcm(960)
        await session.receive_once()
        assert [e["type"] for e in socket.sent] == [
            "session.update", "response.create", "input_audio_buffer.append",
            "conversation.item.truncate",
        ]
        assert socket.sent[-1]["audio_end_ms"] == 20
    asyncio.run(run())


@pytest.mark.parametrize("kwargs,reason", [
    ({}, "disabled"),
    ({"enabled": True}, "not configured"),
])
def test_connection_requires_explicit_opt_in_and_key(kwargs, reason):
    async def run():
        with pytest.raises(LiaVoiceError, match=reason):
            async with open_realtime(**kwargs):
                pytest.fail("must not connect")
    asyncio.run(run())
