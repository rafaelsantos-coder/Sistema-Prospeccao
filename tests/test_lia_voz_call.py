"""Offline SIP + Realtime orchestration tests. No network or phone calls."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import lia_voz_call
from app.services.lia_voz_realtime import LiaVoiceError


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


class FakeDriver:
    def __init__(self):
        self._registration = 200
        self.acc = object()
        self.account_model = object()
        self.invites = 0
        self.hangups = 0
        self.steps = 0
        self.call = None
        self.media = FakeMedia()
        driver = self

        class FakeCall:
            def __init__(self, _account):
                driver.call = self
                self.info = SimpleNamespace(
                    state=0, lastStatusCode=100,
                    media=[SimpleNamespace(type=1, status=1)],
                )

            def getInfo(self):
                return self.info

            def makeCall(self, _target, _params):
                driver.invites += 1

            def getAudioMedia(self, _index):
                return driver.media

            def hangup(self, _params):
                driver.hangups += 1

        self.pj = SimpleNamespace(
            Call=FakeCall, CallOpParam=lambda *_args: SimpleNamespace(),
            PJSIP_INV_STATE_CONFIRMED=2, PJSIP_INV_STATE_DISCONNECTED=3,
            PJMEDIA_TYPE_AUDIO=1, PJSUA_CALL_MEDIA_ACTIVE=1,
            AudioMediaPort=FakePort, MediaFormatAudio=SimpleNamespace,
            ByteVector=list, PJMEDIA_FORMAT_PCM=0,
            PJMEDIA_FRAME_TYPE_AUDIO=1, PJMEDIA_FRAME_TYPE_NONE=0,
        )

    def pump(self, _ms):
        self.steps += 1
        if self.call and self.steps == 1:
            self.call.info.state = 2
            self.call.info.lastStatusCode = 200
            self.call.onCallState(None)
        elif self.call and self.steps == 7:
            self.call.info.state = 3
            self.call.onCallState(None)


class FakeSession:
    ready = True

    def __init__(self):
        self.greeted = False
        self.cleared = False

    async def greet(self):
        self.greeted = True

    async def receive_once(self):
        await asyncio.Future()

    async def append_pcm(self, _pcm):
        pass

    def pull_pcm(self, size):
        return bytes(size)

    def clear_pcm(self):
        self.cleared = True


def factory_for(session):
    @asynccontextmanager
    async def factory(**_kwargs):
        yield session
    return factory


def test_no_invite_when_realtime_handshake_fails():
    driver = FakeDriver()

    @asynccontextmanager
    async def unavailable(**_kwargs):
        raise LiaVoiceError("Handshake failed")
        yield  # pragma: no cover

    async def run():
        with pytest.raises(LiaVoiceError, match="Handshake failed"):
            await lia_voz_call.perform_lia_test(
                driver, "5555999990000", "8003", "test-key",
                should_abort=lambda: False, session_factory=unavailable,
            )
    asyncio.run(run())
    assert driver.invites == 0


def test_no_invite_if_permission_revoked_after_handshake():
    driver = FakeDriver()
    decisions = iter([False, True])

    async def run():
        with pytest.raises(LiaVoiceError, match="authorization revoked"):
            await lia_voz_call.perform_lia_test(
                driver, "5555999990000", "8003", "test-key",
                should_abort=lambda: next(decisions),
                session_factory=factory_for(FakeSession()),
            )
    asyncio.run(run())
    assert driver.invites == 0


def test_unregistered_account_fails_before_invite():
    driver = FakeDriver()
    driver._registration = 403

    async def run():
        with pytest.raises(LiaVoiceError, match="not authorized"):
            await lia_voz_call.perform_lia_test(
                driver, "5555999990000", "8003", "test-key",
                should_abort=lambda: False, session_factory=factory_for(FakeSession()),
            )
    asyncio.run(run())
    assert driver.invites == 0


def test_confirmed_sip_media_and_remote_hangup(monkeypatch):
    driver = FakeDriver()
    session = FakeSession()
    targets = []

    def uri(_account, phone, **options):
        targets.append((phone, options))
        return "sip:fake.invalid"

    monkeypatch.setattr(lia_voz_call, "make_dial_uri", uri)

    async def run():
        return await asyncio.wait_for(lia_voz_call.perform_lia_test(
            driver, "5555999990000", "8003", "test-key",
            should_abort=lambda: False, session_factory=factory_for(session),
        ), timeout=2)

    outcome = asyncio.run(run())
    assert outcome.status == "LIA_FINALIZADA"
    assert outcome.sip_code == 200
    assert "Validar a conversa" in outcome.message
    assert driver.invites == 1 and driver.hangups == 0
    assert targets[0][1]["native_techprefix"] == "8003"
    assert session.greeted and session.cleared
    assert not driver.media.connections
