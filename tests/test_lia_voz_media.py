"""Tests for the native boundary using a fake SWIG API, no phone or network."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.services.lia_voz_media import (
    FRAME_BYTES, SILENCE, MediaBridge, attach_pj_media, create_pj_port,
    run_media_bridge,
)
from app.services.lia_voz_realtime import LiaVoiceError

FRAME = b"\x01\x02" * (FRAME_BYTES // 2)


class Session:
    def __init__(self):
        self.ready = True
        self.sent = []
        self.greeted = False
        self.cleared = False
        self.output = FRAME
        self.receiving = False

    def pull_pcm(self, size):
        assert size == FRAME_BYTES
        return self.output

    def clear_pcm(self):
        self.cleared = True

    async def greet(self):
        self.greeted = True

    async def append_pcm(self, pcm):
        self.sent.append(pcm)

    async def receive_once(self):
        self.receiving = True
        try:
            await asyncio.Future()
        finally:
            self.receiving = False


class Vector(list):
    def size(self):
        return len(self)


class Port:
    def __init__(self):
        self.connected = []

    def createPort(self, name, fmt):
        self.name, self.fmt = name, fmt

    def startTransmit(self, target):
        self.connected.append(target)

    def stopTransmit(self, target):
        self.connected.remove(target)


PJ = SimpleNamespace(
    AudioMediaPort=Port, MediaFormatAudio=SimpleNamespace, ByteVector=Vector,
    PJMEDIA_FORMAT_PCM=1, PJMEDIA_TYPE_AUDIO=1,
    PJMEDIA_FRAME_TYPE_AUDIO=1, PJMEDIA_FRAME_TYPE_NONE=0,
)


def test_port_negotiates_pcm_and_passes_both_directions():
    bridge = MediaBridge(Session())
    port = create_pj_port(PJ, bridge)
    assert (port.fmt.clockRate, port.fmt.channelCount, port.fmt.bitsPerSample,
            port.fmt.frameTimeUsec) == (24000, 1, 16, 20000)
    received = SimpleNamespace(type=1, size=FRAME_BYTES, buf=Vector(FRAME))
    port.onFrameReceived(received)
    assert bridge.next_input() == FRAME
    requested = SimpleNamespace(type=0, size=FRAME_BYTES, buf=Vector())
    port.onFrameRequested(requested)
    assert requested.type == 1 and bytes(requested.buf) == FRAME
    assert bridge.snapshot()["requested_frames"] == 1


def test_capacity_is_bounded_and_overrun_fails_closed():
    bridge = MediaBridge(Session(), input_capacity=2)
    bridge.ingest(FRAME)
    bridge.ingest(FRAME)
    assert bridge.snapshot()["queued_frames"] == 2
    bridge.ingest(FRAME)
    assert bridge.closed and bridge.failure == "Native audio input overrun"
    assert bridge.next_input() is None
    assert bridge.render() == SILENCE


def test_close_is_idempotent_and_late_callbacks_are_silent():
    bridge = MediaBridge(Session())
    bridge.ingest(FRAME)
    bridge.close()
    bridge.close()
    bridge.ingest(FRAME)
    assert bridge.render() == SILENCE
    assert bridge.next_input() is None
    assert bridge.failure is None


def test_malformed_frame_fails_closed():
    bridge = MediaBridge(Session())
    port = create_pj_port(PJ, bridge)
    port.onFrameReceived(SimpleNamespace(type=1, size=3, buf=Vector([1, 2, 3])))
    assert bridge.closed
    assert bridge.failure == "Unexpected native input frame size"


def test_non_audio_frame_is_ignored():
    bridge = MediaBridge(Session())
    port = create_pj_port(PJ, bridge)
    port.onFrameReceived(SimpleNamespace(type=0, size=0, buf=Vector()))
    assert not bridge.closed
    assert bridge.next_input() is None


def test_invalid_output_capacity_is_rejected():
    bridge = MediaBridge(Session())
    port = create_pj_port(PJ, bridge)
    requested = SimpleNamespace(type=1, size=10, buf=Vector())
    port.onFrameRequested(requested)
    assert requested.type == 0 and requested.size == 0
    assert bridge.closed


def test_bad_output_source_produces_silence():
    session = Session()
    session.output = b"bad"
    bridge = MediaBridge(session)
    assert bridge.render() == SILENCE
    assert bridge.failure == "Native audio output failed"


def test_attach_is_opt_in_and_disconnects_both_directions():
    bridge = MediaBridge(Session())
    call_audio = Port()
    with pytest.raises(LiaVoiceError):
        with attach_pj_media(PJ, call_audio, bridge):
            pytest.fail("must not connect")
    with attach_pj_media(PJ, call_audio, bridge, enabled=True) as port:
        assert call_audio.connected == [port]
        assert port.connected == [call_audio]
    assert not port.connected and not call_audio.connected and bridge.closed


def test_two_calls_never_share_buffers():
    a, b = MediaBridge(Session()), MediaBridge(Session())
    a.ingest(FRAME)
    assert b.next_input() is None
    a.close()
    assert not b.closed and b.render() == FRAME


def test_native_callback_and_async_socket_cleanup():
    async def run():
        session = Session()
        bridge = MediaBridge(session)
        task = asyncio.create_task(run_media_bridge(session, bridge))
        worker = threading.Thread(target=bridge.ingest, args=(FRAME,))
        worker.start()
        worker.join()
        async with asyncio.timeout(1):
            while not session.sent or not session.receiving:
                await asyncio.sleep(0.005)
        bridge.close()
        await asyncio.wait_for(task, 1)
        assert session.sent == [FRAME]
        assert session.greeted and session.cleared and not session.receiving
    asyncio.run(run())


def test_transport_failure_ends_test_without_retry():
    class BrokenSession(Session):
        async def append_pcm(self, pcm):
            raise RuntimeError("sensitive message")
    async def run():
        session = BrokenSession()
        bridge = MediaBridge(session)
        bridge.ingest(FRAME)
        with pytest.raises(LiaVoiceError, match="Realtime media transport failed"):
            await asyncio.wait_for(run_media_bridge(session, bridge), 1)
        assert bridge.closed and session.cleared and not session.receiving
    asyncio.run(run())


def test_overrun_during_test_is_reported_and_tasks_are_joined():
    async def run():
        session = Session()
        bridge = MediaBridge(session, input_capacity=1)
        task = asyncio.create_task(run_media_bridge(session, bridge))
        async with asyncio.timeout(1):
            while not session.receiving:
                await asyncio.sleep(0.005)
        bridge.ingest(FRAME)
        bridge.ingest(FRAME)
        with pytest.raises(LiaVoiceError, match="Native audio input overrun"):
            await asyncio.wait_for(task, 1)
        assert session.cleared and not session.receiving
    asyncio.run(run())


def test_disconnect_race_after_remote_bye_preserves_result():
    class GonePort(Port):
        def stopTransmit(self, _target):
            raise RuntimeError("remote conference port removed")
    for remote_ended in (True, False):
        bridge = MediaBridge(Session())
        call_audio = GonePort()
        if remote_ended:
            with attach_pj_media(PJ, call_audio, bridge, enabled=True,
                                 remote_ended=lambda: True):
                pass
        else:
            with pytest.raises(LiaVoiceError, match="Native audio disconnect failed"):
                with attach_pj_media(PJ, call_audio, bridge, enabled=True):
                    pass
        assert bridge.closed


def test_transport_failure_preserves_safe_category():
    from app.services.lia_voz_realtime import safe_realtime_failure
    class BrokenSession(Session):
        async def receive_once(self):
            raise TimeoutError("never log this content")

    async def run():
        session = BrokenSession()
        bridge = MediaBridge(session)
        with pytest.raises(LiaVoiceError) as raised:
            await asyncio.wait_for(run_media_bridge(session, bridge), 1)
        assert safe_realtime_failure(raised.value) == "transport_timeout"
        assert "never log" not in str(raised.value)
        assert bridge.closed
    asyncio.run(run())


def test_media_latency_metrics_are_local_and_do_not_retain_audio():
    clock = [100.0]
    bridge = MediaBridge(Session(), clock=lambda: clock[0])
    bridge.ingest(FRAME)
    clock[0] += 0.02
    bridge.ingest(FRAME)
    clock[0] += 0.03
    assert bridge.next_input() == FRAME
    assert bridge.render() == FRAME
    clock[0] += 0.04
    bridge.render()
    bridge.record_send(12)
    snap = bridge.snapshot()
    assert snap["input_peak_ms"] == 40
    assert snap["input_wait_max_ms"] == 50
    assert snap["input_gap_max_ms"] == 20
    assert snap["render_gap_max_ms"] == 40
    assert snap["first_voice_ms"] == 50
    assert snap["send_max_ms"] == 12
    assert not any(isinstance(v, bytes) for v in snap.values())
