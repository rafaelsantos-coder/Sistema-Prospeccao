"""Offline unit tests: no API key, network, SIP or external calls."""
import asyncio
import base64
import json
from collections import deque

import pytest

from app.services.lia_voz_realtime import (
    AudioItem, LiaVoiceError, MAX_BUFFER_BYTES, MAX_RESPONSE_TOKENS, MODEL,
    PCM_BYTES_PER_SECOND, Playback,
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
    assert config["max_output_tokens"] == MAX_RESPONSE_TOKENS
    assert config["tools"] == []
    assert "Eduarda Voz" in config["instructions"]
    assert "Você é Eduarda, assistente virtual comercial da Sulnet" in config["instructions"]
    assert "Você é Lia," not in config["instructions"]
    assert config["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert config["model"] == "gpt-realtime-2.1"
    assert config["reasoning"] == {"effort": "medium"}
    assert config["audio"]["input"]["noise_reduction"] == {"type": "near_field", "strength": 0.6}
    assert config["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad", "eagerness": "low",
        "create_response": True, "interrupt_response": True,
    }


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
        assert "Apresente-se como Eduarda, assistente virtual da Sulnet" in socket.sent[-1]["response"]["instructions"]
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


def test_provider_errors_are_diagnostic_but_never_expose_raw_payload():
    from app.services.lia_voz_realtime import safe_realtime_failure
    for code, expected in [
        ("rate_limit_exceeded", "provider:rate_limit_exceeded"),
        ("private-value-123", "provider_error"),
    ]:
        with pytest.raises(LiaVoiceError) as raised:
            RealtimeProtocol().handle({
                "type": "error", "error": {"code": code, "message": "private customer content"}
            })
        assert safe_realtime_failure(raised.value) == expected
        assert "private" not in str(raised.value)
    with pytest.raises(LiaVoiceError) as raised:
        RealtimeProtocol().handle({
            "type": "response.done",
            "response": {"id": "r1", "status": "incomplete",
                         "status_details": {"reason": "content_filter"}},
        })
    assert safe_realtime_failure(raised.value) == "provider:content_filter"


def test_quality_metrics_for_turns_interruption_and_generation(monkeypatch):
    from app.services import lia_voz_realtime
    clock = iter([10.0, 10.35, 11.0, 11.5])
    monkeypatch.setattr(lia_voz_realtime.time, "monotonic", lambda: next(clock))
    p = RealtimeProtocol()
    p.handle(SPEECH)
    p.handle({"type": "input_audio_buffer.speech_stopped"})
    p.handle(created())
    p.handle(delta())
    p.handle(delta())
    p.handle(SPEECH)
    p.handle(delta())  # cancelled response, no additional audio or latency
    p.handle({"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    p.handle({"type": "input_audio_buffer.speech_stopped"})
    p.handle(created("r2"))
    p.handle(delta("r2", "i2"))
    p.handle({"type": "response.done", "response": {"id": "r2", "status": "completed"}})
    assert p.quality_snapshot() == {
        "speech_starts": 2, "speech_ends": 2, "interruptions": 1,
        "completed_responses": 1, "audio_responses": 2,
        "generation_samples": 2, "generation_avg_ms": 425,
        "generation_max_ms": 500,
        "buffer_recoveries": 0,
        "overlap_recoveries": 0,
        "token_limited_responses": 0,
        "peak_pending_ms": 40,
        "playback_underruns": 0,
        "playback_rebuffer_ms": 0,
    }


def test_commercial_training_v3_keeps_all_nine_negotiation_steps():
    from app.services.lia_voz_realtime import PILOT_TRAINING, PILOT_TRAINING_VERSION

    assert PILOT_TRAINING_VERSION == "3.0"
    assert all(f"## {i}. " in PILOT_TRAINING for i in range(1, 23))
    for row in (
        "| 1 | 800 mega | R$ 119,90 | Nenhuma |",
        "| 2 | 800 mega | R$ 119,90 | 1 mês |",
        "| 3 | 800 mega | R$ 119,90 | 2 meses |",
        "| 4 | 800 mega | R$ 119,90 | 3 meses |",
        "| 5 | 650 mega | R$ 99,90 | Nenhuma |",
        "| 6 | 800 mega | R$ 99,90 | Nenhuma |",
        "| 7 | 800 mega | R$ 99,90 | 1 mês |",
        "| 8 | 800 mega | R$ 99,90 | 2 meses |",
        "| 9 | 800 mega | R$ 99,90 | 3 meses |",
    ):
        assert row in PILOT_TRAINING
    assert "Todas as mensalidades gratuitas são concedidas no início do contrato." in PILOT_TRAINING
    assert "Todas as ofertas previstas possuem fidelidade de 12 meses." in PILOT_TRAINING


def test_training_is_loaded_with_pilot_guard_and_local_date(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services import lia_voz_realtime as lia

    monkeypatch.setattr(lia, "_local_now", lambda: datetime(
        2026, 11, 27, 21, 30, tzinfo=timezone(timedelta(hours=-3))
    ))
    config = lia.session_update()["session"]
    instructions = config["instructions"]
    assert instructions.startswith(lia.PILOT_GUARD)
    assert lia.PILOT_TRAINING in instructions
    assert instructions.count(lia.PILOT_GUARD) == 1
    assert instructions.count(lia.PILOT_TRAINING) == 1
    assert "Versão 3.0" in instructions
    assert "Versão 2.2" not in instructions
    assert "Não solicite CPF" in instructions
    assert "Não confirme cobertura" in instructions
    assert "27/11/2026 21:30" in instructions
    assert config["tools"] == []



def test_training_v3_has_one_consistent_permission_based_recovery():
    from app.services import lia_voz_realtime as lia

    instructions = lia.session_update()["session"]["instructions"]
    assert "não gostaria agora" in lia.PILOT_GUARD
    assert "não é recusa definitiva" in lia.PILOT_GUARD
    assert "nova negativa após a única tentativa" in lia.PILOT_GUARD
    assert "Faça UMA tentativa breve de recuperação" in lia.PILOT_TRAINING
    assert "Recusa inequívoca" in lia.PILOT_TRAINING
    assert "segunda negativa à tentativa de recuperação" in lia.PILOT_TRAINING
    assert "Áudio ou frase incompleta" in lia.PILOT_TRAINING
    assert "não encerre por falha de comunicação" in instructions
    assert "Não solicite CPF" in instructions
    assert "não afirme que gravou o opt-out" in instructions


def test_token_limit_is_normal_and_queued_audio_is_preserved():
    p = RealtimeProtocol()
    p.handle(created())
    pcm = b"\x11\x22" * 480
    p.handle(delta(pcm=pcm))
    assert p.handle({
        "type": "response.done",
        "response": {"id": "r1", "status": "incomplete",
                     "status_details": {"reason": "max_output_tokens"}},
    }) == []
    assert p.active_response is None
    assert p.playback.pull(960) == pcm
    assert p.quality_snapshot()["token_limited_responses"] == 1
    p.handle(created("r2"))
    p.handle(delta("r2", "i2", pcm))
    assert p.playback.pull(960) == pcm


def test_overflow_limits_only_response_and_keeps_phone_playback():
    p = RealtimeProtocol()
    p.handle(created())
    pcm = b"\x01\x02"
    p.handle(delta(pcm=pcm * ((MAX_BUFFER_BYTES - 960) // 2)))
    assert p.playback.pull(960) == pcm * 480
    events = p.handle(delta(pcm=pcm * 1440))
    assert events == [
        {"type": "response.cancel", "response_id": "r1", "event_id": "lia-buffer-cancel-1"},
        {"type": "conversation.item.truncate", "item_id": "i1", "content_index": 0,
         "audio_end_ms": (MAX_BUFFER_BYTES + 960) * 1000 // PCM_BYTES_PER_SECOND},
    ]
    assert len(p.playback.pending) == MAX_BUFFER_BYTES
    # Late deltas after cancellation cannot fill memory or trigger another error.
    for _ in range(20):
        assert p.handle(delta(pcm=pcm * 480)) == []
    assert p.quality_snapshot()["buffer_recoveries"] == 1
    assert p.quality_snapshot()["peak_pending_ms"] == 30000
    assert p.playback.pull(960) == pcm * 480
    # Server completed first: only the correlated cancel race is recoverable.
    assert p.handle({
        "type": "error", "error": {"code": "response_cancel_not_active",
                                  "event_id": events[0]["event_id"]},
    }) == []
    p.handle({"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    assert p.handle(SPEECH) == [{
        "type": "conversation.item.truncate", "item_id": "i1",
        "content_index": 0, "audio_end_ms": 40,
    }]
    assert p.playback.pull(960) == bytes(960)
    p.handle(created("r2"))
    p.handle(delta("r2", "i2", b"\x05\x06" * 480))
    assert p.playback.pull(960) == b"\x05\x06" * 480


def test_oversized_first_delta_has_bounded_prefix_and_no_disconnect():
    p = RealtimeProtocol()
    p.handle(created())
    events = p.handle(delta(pcm=b"\x01\x02" * (MAX_BUFFER_BYTES // 2 + 480)))
    assert len(p.playback.pending) == MAX_BUFFER_BYTES
    assert events[0]["response_id"] == "r1"
    assert events[1]["audio_end_ms"] == 30000


def test_uncorrelated_cancel_error_is_not_silently_ignored():
    with pytest.raises(LiaVoiceError) as raised:
        RealtimeProtocol().handle({
            "type": "error",
            "error": {"code": "response_cancel_not_active", "event_id": "unrelated"},
        })
    assert raised.value.diagnostic == "provider:response_cancel_not_active"


def test_reproduce_three_turn_call_with_burst_and_recovery_over_websocket():
    """Reproduce production test #20 using synthetic provider events only."""
    async def run():
        socket = FakeSocket([
            {"type": "session.updated"},
            created(), delta(pcm=b"\x01\x02" * 480),
            {"type": "response.done", "response": {"id": "r1", "status": "completed"}},
            SPEECH, {"type": "input_audio_buffer.speech_stopped"},
            created("r2"),
            delta("r2", "i2", b"\x03\x04" * (MAX_BUFFER_BYTES // 2)),
            delta("r2", "i2", b"\x03\x04" * 480),
            {"type": "response.done", "response": {"id": "r2", "status": "cancelled"}},
            SPEECH, {"type": "input_audio_buffer.speech_stopped"},
            created("r3"), delta("r3", "i3", b"\x05\x06" * 480),
            {"type": "response.done", "response": {"id": "r3", "status": "completed"}},
        ])
        session = RealtimeSession(socket)
        await session.start()
        for _ in range(3):
            await session.receive_once()
        assert session.pull_pcm(960) == b"\x01\x02" * 480
        for _ in range(6):
            await session.receive_once()
        assert session.ready
        assert session.pull_pcm(960) == b"\x03\x04" * 480
        await session.append_pcm(bytes(960))
        for _ in range(5):
            await session.receive_once()
        assert session.ready
        assert session.pull_pcm(960) == b"\x05\x06" * 480
        assert session.quality_snapshot()["buffer_recoveries"] == 1
        assert session.quality_snapshot()["audio_responses"] == 3
        assert [e["type"] for e in socket.sent].count("response.cancel") == 1
    asyncio.run(run())


def test_new_response_preempts_unplayed_audio_without_dropping_session():
    """Regression for production test #22: provider starts another audio item."""
    p = RealtimeProtocol()
    p.handle(created("r1"))
    p.handle(delta("r1", "i1", b"\x01\x02" * 1440))
    assert p.playback.pull(960) == b"\x01\x02" * 480
    p.handle(created("r2"))
    # Delayed deltas from the previous generation must not revive it.
    assert p.handle(delta("r1", "i1", b"\x03\x04" * 480)) == []
    assert p.handle(delta("r2", "i2", b"\x05\x06" * 960)) == [{
        "type": "conversation.item.truncate", "item_id": "i1",
        "content_index": 0, "audio_end_ms": 20,
    }]
    assert p.playback.pull(960) == b"\x05\x06" * 480
    p.handle({"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    assert p.active_response == "r2"
    assert p.handle(SPEECH) == [{
        "type": "conversation.item.truncate", "item_id": "i2",
        "content_index": 0, "audio_end_ms": 20,
    }]
    p.handle(created("r3"))
    p.handle(delta("r3", "i3", b"\x07\x08" * 480))
    assert p.playback.pull(960) == b"\x07\x08" * 480
    assert p.quality_snapshot()["overlap_recoveries"] == 1


def test_multiple_audio_items_in_one_response_keep_latest_without_mixing():
    p = RealtimeProtocol()
    p.handle(created())
    p.handle(delta(item_id="i1", pcm=b"\x01\x02" * 960))
    p.playback.pull(960)
    assert p.handle(delta(item_id="i2", pcm=b"\x03\x04" * 960)) == [{
        "type": "conversation.item.truncate", "item_id": "i1",
        "content_index": 0, "audio_end_ms": 20,
    }]
    assert p.handle(delta(item_id="i1", pcm=b"\x05\x06" * 480)) == []
    assert p.playback.pull(960) == b"\x03\x04" * 480
    assert p.handle(delta(item_id="i2", pcm=b"\x07\x08" * 480)) == []
    assert p.playback.pull(960) == b"\x03\x04" * 480
    assert p.playback.pull(960) == b"\x07\x08" * 480
    assert p.active_response == "r1"
    assert p.quality_snapshot()["overlap_recoveries"] == 1


def test_overlapping_turns_recover_through_websocket_adapter():
    async def run():
        socket = FakeSocket([
            {"type": "session.updated"}, created(),
            delta(pcm=b"\x01\x02" * 960),
            {"type": "response.done", "response": {"id": "r1", "status": "completed"}},
            created("r2"), delta("r2", "i2", b"\x03\x04" * 960),
            SPEECH, {"type": "input_audio_buffer.speech_stopped"},
            created("r3"), delta("r3", "i3", b"\x05\x06" * 480),
        ])
        session = RealtimeSession(socket)
        await session.start()
        for _ in range(3):
            await session.receive_once()
        session.pull_pcm(960)
        for _ in range(2):
            await session.receive_once()
        assert session.pull_pcm(960) == b"\x03\x04" * 480
        assert session.ready
        await session.append_pcm(bytes(960))
        for _ in range(4):
            await session.receive_once()
        assert session.pull_pcm(960) == b"\x05\x06" * 480
        assert session.ready
        assert session.quality_snapshot()["overlap_recoveries"] == 1
        assert [e["audio_end_ms"] for e in socket.sent
                if e["type"] == "conversation.item.truncate"] == [20, 20]
    asyncio.run(run())


def test_streamed_playback_prebuffers_rebuffers_and_drains_without_losing_samples():
    p = Playback(prebuffer_ms=160)
    item = AudioItem("r", "i", 0)
    frame = b"\x01\x02" * 480
    p.push(item, frame * 7)
    assert p.pull(960, streaming=True) == bytes(960)
    assert p.played_bytes == 0
    p.push(item, frame)
    for _ in range(8):
        assert p.pull(960, streaming=True) == frame
    assert p.pull(960, streaming=True) == bytes(960)
    assert p.underruns == 1
    p.push(item, frame * 2)
    assert p.pull(960, streaming=True) == bytes(960)
    assert p.rebuffer_bytes == 1920
    # Completed turn drains its short tail immediately, without another threshold.
    assert p.pull(960, streaming=False) == frame
    assert p.pull(960, streaming=False) == frame
    assert p.interrupt()["audio_end_ms"] == 200


def test_true_interruption_still_discards_buffered_and_late_audio():
    p = RealtimeProtocol(prebuffer_ms=160)
    p.handle(created())
    frame = b"\x01\x02" * 480
    p.handle(delta(pcm=frame * 10))
    assert p.playback.pull(960, streaming=True) == frame
    assert p.handle(SPEECH)[0]["audio_end_ms"] == 20
    assert not p.playback.pending
    assert p.handle(delta(pcm=frame)) == []
    assert p.playback.pull(960, streaming=True) == bytes(960)


def test_session_profile_confirmation_and_configurable_model():
    async def run():
        cfg = session_update("gpt-realtime-2.1-mini", reasoning_effort="high")["session"]
        socket = FakeSocket([{"type": "session.updated", "session": cfg}])
        session = RealtimeSession(socket, "gpt-realtime-2.1-mini", reasoning_effort="high")
        await session.start()
        assert session.profile()["confirmed"]
        assert session.profile()["reasoning"] == "high"
        assert session.profile()["model"] == "gpt-realtime-2.1-mini"
        assert socket.sent[0]["session"]["reasoning"] == {"effort": "high"}
    asyncio.run(run())
    with pytest.raises(ValueError):
        session_update("unapproved-model")
    with pytest.raises(ValueError):
        session_update(reasoning_effort="unapproved")
