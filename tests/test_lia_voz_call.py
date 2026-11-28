"""Regression exercises the real shared PjSession path with fake native I/O."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import lia_voz_call, voice_pjsua2
from app.services.lia_voz_realtime import LiaVoiceError
from app.services.lia_voz_media import FRAME_BYTES


class FakeVector(list):
    def size(self):
        return len(self)


class FakeMedia:
    def __init__(self):
        self.connections = []

    def startTransmit(self, target):
        self.connections.append(target)

    def stopTransmit(self, target):
        self.connections.remove(target)


class FakePort(FakeMedia):
    def createPort(self, _name, _fmt):
        pass


class FakeDriver(voice_pjsua2.PjSession):
    def __init__(self, *, answer_at=3, remote_after=5, emit_pcm=True, rtp=(10, 10)):
        self._registration = 200
        self.acc = object()
        self.account_model = object()
        self.invites = []
        self.hangups = 0
        self.steps = 0
        self.clock = 0.0
        self.call = None
        self._call = None
        self._player = None
        self.answer_at = answer_at
        self.remote_after = remote_after
        self.emit_pcm = emit_pcm
        self.rtp = rtp
        self.media = FakeMedia()
        driver = self

        class FakeCall:
            def __init__(self, _account):
                driver.call = self
                self.info = SimpleNamespace(
                    state=0, lastStatusCode=100, callIdString="local-lab-123@loopback",
                    media=[SimpleNamespace(type=1, status=1, index=0)],
                )

            def getInfo(self):
                return self.info

            def makeCall(self, target, params):
                driver.invites.append((target, params))

            def getAudioMedia(self, index):
                assert index == 0
                return driver.media

            def getStreamStat(self, _index):
                return SimpleNamespace(rtcp=SimpleNamespace(
                    rxStat=SimpleNamespace(pkt=driver.rtp[0]),
                    txStat=SimpleNamespace(pkt=driver.rtp[1])))

            def hangup(self, _params):
                driver.hangups += 1

        self.pj = SimpleNamespace(
            Call=FakeCall, CallOpParam=lambda *_args: SimpleNamespace(opt=SimpleNamespace()),
            PJSIP_INV_STATE_CONFIRMED=2, PJSIP_INV_STATE_DISCONNECTED=3,
            PJMEDIA_TYPE_AUDIO=1, PJSUA_CALL_MEDIA_ACTIVE=1,
            AudioMediaPort=FakePort, MediaFormatAudio=SimpleNamespace,
            ByteVector=FakeVector, PJMEDIA_FORMAT_PCM=0,
            PJMEDIA_FRAME_TYPE_AUDIO=1, PJMEDIA_FRAME_TYPE_NONE=0,
        )

    def pump(self, ms):
        if ms == 100:
            return
        self.steps += 1
        self.clock += 1 if ms == 150 else 0.2
        if self.call and self.steps == 1:
            self.call.info.state = 1
            self.call.info.lastStatusCode = 183
            self.call.onCallState(None)
        if self.call and self.steps == self.answer_at:
            self.call.info.state = 2
            self.call.info.lastStatusCode = 200
            self.call.onCallState(None)
            self.call.onCallMediaState(None)
        if self.call and self.emit_pcm and self.media.connections:
            port = self.media.connections[0]
            port.onFrameReceived(SimpleNamespace(type=1, size=FRAME_BYTES,
                                                buf=FakeVector([1] * FRAME_BYTES)))
            port.onFrameRequested(SimpleNamespace(size=FRAME_BYTES, buf=FakeVector(), type=0))
        if self.call and self.steps == self.answer_at + self.remote_after:
            self.call.info.state = 3
            self.call.onCallState(None)


class FakeSession:
    ready = True

    def __init__(self, *, silent=False):
        self.greeted = False
        self.cleared = False
        self.silent = silent

    async def greet(self):
        self.greeted = True

    async def receive_once(self):
        await asyncio.Future()

    async def append_pcm(self, _pcm):
        pass

    def pull_pcm(self, size):
        return bytes(size) if self.silent else bytes([1]) * size

    def clear_pcm(self):
        self.cleared = True


def install_driver(monkeypatch, **kw):
    driver = FakeDriver(**kw)
    monkeypatch.setattr(voice_pjsua2, "time", SimpleNamespace(monotonic=lambda: driver.clock))
    monkeypatch.setattr(lia_voz_call, "time", SimpleNamespace(monotonic=lambda: driver.clock))
    routes = []
    def uri(_account, phone, **options):
        routes.append((phone, options))
        return "sip:lab.invalid"
    monkeypatch.setattr(voice_pjsua2, "make_dial_uri", uri)
    return driver, routes


def run_lia(driver, session=None, *, prefix=None, should_abort=lambda: False, progress=None):
    @asynccontextmanager
    async def factory(**_kwargs):
        assert driver.call.answered
        assert len(driver.invites) == 1
        yield session or FakeSession()
    return lia_voz_call.perform_lia_test(
        driver, "5555999990000", prefix, "test-key",
        should_abort=should_abort, session_factory=factory,
        on_progress=lambda message, code: progress.append((message, code)) if progress is not None else None,
    )


@pytest.mark.parametrize("registration,key,aborted", [(403, "key", False), (200, "", False), (200, "key", True)])
def test_preflight_denial_never_invites(monkeypatch, registration, key, aborted):
    driver, _ = install_driver(monkeypatch)
    driver._registration = registration
    with pytest.raises(LiaVoiceError, match="not authorized"):
        lia_voz_call.perform_lia_test(driver, "5555999990000", None, key,
                                      should_abort=lambda: aborted)
    assert not driver.invites


def test_same_native_dialer_and_parameters_for_probe_and_lia(monkeypatch):
    driver, probe_routes = install_driver(monkeypatch)
    probe = lia_voz_call.perform_native_probe(driver, "5555999990000", None, should_abort=lambda: False)
    assert probe.status == "LIA_PROBE_FINALIZADO"
    assert len(driver.invites) == 1
    driver, routes = install_driver(monkeypatch)
    session = FakeSession()
    progress = []
    result = run_lia(driver, session, progress=progress)
    assert result.status == "LIA_FINALIZADA"
    assert result.sip_code == 200
    assert "RTP pacotes RX/TX=10/10" in result.message
    assert routes == probe_routes
    assert routes[0][1]["dial_format"] == "NATIONAL"
    assert len(driver.invites) == 1
    assert driver.hangups == 0 and driver._call is None
    assert session.greeted and session.cleared
    assert not driver.media.connections
    assert any("Call-ID=local-lab-123@loopback" in m for m, _ in progress)


@pytest.mark.parametrize("answer_at,expected", [(32, "LIA_FINALIZADA"), (42, "LIA_SEM_ATENDIMENTO")])
def test_delayed_answer_or_timeout(monkeypatch, answer_at, expected):
    driver, _ = install_driver(monkeypatch, answer_at=answer_at)
    result = run_lia(driver)
    assert result.status == expected
    assert len(driver.invites) == 1
    if expected == "LIA_SEM_ATENDIMENTO":
        assert result.sip_code == 183
        assert driver.hangups == 1
        assert driver.clock == 40


@pytest.mark.parametrize("emit_pcm,rtp,silent", [(False, (10, 10), False), (True, (0, 10), False), (True, (10, 10), True)])
def test_no_success_on_silent_or_missing_network_audio(monkeypatch, emit_pcm, rtp, silent):
    driver, _ = install_driver(monkeypatch, emit_pcm=emit_pcm, rtp=rtp)
    result = run_lia(driver, FakeSession(silent=silent))
    assert result.status == "LIA_FALHOU"
    assert len(driver.invites) == 1


def test_native_pump_continues_during_handshake(monkeypatch):
    driver, _ = install_driver(monkeypatch, remote_after=20)
    @asynccontextmanager
    async def delayed(**_kwargs):
        assert driver.call.answered
        before = driver.steps
        await asyncio.sleep(0.04)
        assert driver.steps > before
        yield FakeSession()
    result = lia_voz_call.perform_lia_test(
        driver, "5555999990000", None, "test-key", should_abort=lambda: False,
        session_factory=delayed)
    assert result.status == "LIA_FINALIZADA"
    assert len(driver.invites) == 1


def test_realtime_failure_after_answer_hangs_up(monkeypatch):
    driver, _ = install_driver(monkeypatch, remote_after=20)
    @asynccontextmanager
    async def failed(**_kwargs):
        assert driver.call.answered
        raise LiaVoiceError("private content", diagnostic="provider:rate_limit_exceeded")
        yield
    result = lia_voz_call.perform_lia_test(
        driver, "5555999990000", None, "test-key", should_abort=lambda: False,
        session_factory=failed)
    assert result.status == "LIA_FALHOU"
    assert "provider:rate_limit_exceeded" in result.message
    assert "private content" not in result.message
    assert len(driver.invites) == 1 and driver.hangups == 1


def test_remote_hangup_during_handshake_cancels_connection(monkeypatch):
    driver, _ = install_driver(monkeypatch, remote_after=2)
    cancelled = []
    @asynccontextmanager
    async def waiting(**_kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)
        yield
    result = lia_voz_call.perform_lia_test(
        driver, "5555999990000", None, "test-key", should_abort=lambda: False,
        session_factory=waiting)
    assert result.status == "LIA_FALHOU"
    assert cancelled == [True]
    assert len(driver.invites) == 1 and driver.hangups == 0


def test_guard_revocation_before_answer_does_not_open_realtime(monkeypatch):
    driver, _ = install_driver(monkeypatch, answer_at=10)
    result = run_lia(driver, should_abort=lambda: driver.steps >= 2)
    assert result.status == "LIA_INTERROMPIDA"
    assert len(driver.invites) == 1 and driver.hangups == 1


def test_native_route_still_uses_authorized_prefix(monkeypatch):
    driver, routes = install_driver(monkeypatch)
    result = run_lia(driver, prefix="8003")
    assert result.status == "LIA_FINALIZADA"
    assert routes[0][1]["native_techprefix"] == "8003"
    assert len(driver.invites) == 1
