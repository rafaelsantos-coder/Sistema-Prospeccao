"""Parada graciosa dos dois workers SIP, sem originar ligacoes reais."""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from app.workers import voice_engine_worker, voice_test_worker


class FakeStop:
    requested = False
    def install(self):
        pass
    def wait(self, _seconds):
        return self.requested


def test_voice_engine_finishes_current_call_before_handover(monkeypatch):
    stop = FakeStop()
    completed = []
    leader = SimpleNamespace(assert_held=lambda **_kw: None, healthy=lambda: True)
    monkeypatch.setenv("VOICE_ENGINE_WORKER_ENABLED", "true")
    monkeypatch.setattr(voice_engine_worker, "GracefulShutdown", lambda _log: stop)
    monkeypatch.setattr(
        voice_engine_worker, "voice_engine_leadership", lambda: nullcontext(leader)
    )
    monkeypatch.setattr(voice_engine_worker, "_expire_crashed_calls", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "heartbeat", lambda *_a: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_native_test", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_transfer_test", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_recipient", lambda: 77)

    def fake_call(recipient_id, real_leader):
        assert real_leader is leader
        completed.append(recipient_id)
        stop.requested = True

    monkeypatch.setattr(voice_engine_worker, "_run_campaign_call", fake_call)
    voice_engine_worker.main()
    assert completed == [77]


def test_voice_test_worker_drains_claimed_diagnostic(monkeypatch):
    stop = FakeStop()
    calls = []
    monkeypatch.setenv("VOICE_TEST_WORKER_ENABLED", "true")
    monkeypatch.setattr(voice_test_worker, "GracefulShutdown", lambda _log: stop)
    monkeypatch.setattr(voice_test_worker, "heartbeat", lambda: None)
    monkeypatch.setattr(voice_test_worker, "expire_stale", lambda: None)
    monkeypatch.setattr(voice_test_worker, "claim_next", lambda: (11, 22, "SIP_ONLY"))

    def finish(*args):
        calls.append(args)
        stop.requested = True

    monkeypatch.setattr(voice_test_worker, "run_one", finish)
    voice_test_worker.main()
    assert calls == [(11, 22, "SIP_ONLY")]
