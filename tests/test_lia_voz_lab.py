"""Offline checks of lab controls. No phone, DB, API or secrets."""
from contextlib import asynccontextmanager
import asyncio

import pytest

# PJSUA2 is installed only in the native image, not the main test runtime.
from app.services.lia_voz_realtime import LiaVoiceError, RealtimeProtocol


def test_probe_realtime_needs_usable_audio(monkeypatch):
    from scripts import lia_voz_lab as lab

    class Session:
        protocol = RealtimeProtocol()

        async def greet(self):
            pass

        async def receive_once(self):
            return "response.done"

        def pull_pcm(self, size):
            return bytes(size)

    @asynccontextmanager
    async def factory(**_kwargs):
        yield Session()

    with pytest.raises(LiaVoiceError, match="usable audio"):
        asyncio.run(lab.probe_realtime("dummy-test", session_factory=factory))


def test_lab_default_never_connects(monkeypatch):
    from scripts import lia_voz_lab as lab
    monkeypatch.setattr(lab, "native_bench", lambda: None)
    monkeypatch.delenv("LIA_VOZ_LAB_REALTIME_ENABLED", raising=False)
    monkeypatch.delenv("LIA_VOZ_OPENAI_API_KEY", raising=False)
    assert lab.main() == 0


def test_enabled_without_key_blocks(monkeypatch):
    from scripts import lia_voz_lab as lab
    monkeypatch.setattr(lab, "native_bench", lambda: None)
    monkeypatch.setenv("LIA_VOZ_LAB_REALTIME_ENABLED", "true")
    monkeypatch.delenv("LIA_VOZ_OPENAI_API_KEY", raising=False)
    assert lab.main() == 2
