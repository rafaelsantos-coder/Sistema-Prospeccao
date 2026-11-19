"""Encerramento cooperativo, sem chamadas SIP ou envios reais."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import textwrap
from contextlib import nullcontext
from types import SimpleNamespace

from app.workers import campaign_worker, voice_engine_worker, voice_test_worker
from app.workers.graceful_shutdown import GracefulShutdown


class FakeStop:
    requested = False

    def install(self):
        pass

    def wait(self, seconds):
        return self.requested


class FakeDb:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def test_sigterm_sets_shutdown_without_raising(monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, callback: handlers.__setitem__(sig, callback))
    stop = GracefulShutdown(logging.getLogger("test-shutdown"))
    stop.install()
    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.requested
    assert stop.wait(0)


def test_campaign_rolls_back_claim_if_shutdown_precedes_send(monkeypatch):
    stop, db = FakeStop(), FakeDb()
    calls = []
    monkeypatch.setenv("CAMPAIGN_DISPATCH_PAUSED", "false")
    monkeypatch.setattr(campaign_worker, "GracefulShutdown", lambda logger: stop)
    monkeypatch.setattr(campaign_worker, "SessionLocal", lambda: db)

    def claim(_db):
        calls.append("claim")
        stop.requested = True
        return SimpleNamespace(id=1, dispatch_id=2)

    monkeypatch.setattr(campaign_worker, "claim_next_dispatch_recipient", claim)
    monkeypatch.setattr(
        campaign_worker, "process_dispatch_recipient",
        lambda *_args: calls.append("send"),
    )
    campaign_worker.run()
    assert calls == ["claim"]
    assert db.rollbacks == 1
    assert db.commits == 0
    assert db.closes == 1


def test_campaign_commits_result_before_stopping(monkeypatch):
    stop, db = FakeStop(), FakeDb()
    calls = []
    monkeypatch.setenv("CAMPAIGN_DISPATCH_PAUSED", "false")
    monkeypatch.setattr(campaign_worker, "GracefulShutdown", lambda logger: stop)
    monkeypatch.setattr(campaign_worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        campaign_worker,
        "claim_next_dispatch_recipient",
        lambda _db: SimpleNamespace(id=7, dispatch_id=8, attempts=1),
    )

    def send(_db, _row):
        # O claim precisa ter commit antes de qualquer efeito externo.
        assert db.commits == 1
        calls.append("sent")
        stop.requested = True
        return "sent"

    monkeypatch.setattr(campaign_worker, "process_dispatch_recipient", send)
    campaign_worker.run()
    assert calls == ["sent"]
    assert db.commits == 2  # reserva duravel e resultado final
    assert db.rollbacks == 0
    assert db.closes == 1


def test_voice_engine_finishes_claimed_call_without_new_claims(monkeypatch):
    stop = FakeStop()
    calls = []
    monkeypatch.setenv("VOICE_ENGINE_WORKER_ENABLED", "true")
    monkeypatch.setattr(voice_engine_worker, "GracefulShutdown", lambda logger: stop)
    leader = SimpleNamespace(assert_held=lambda **_kwargs: None, healthy=lambda: True)
    monkeypatch.setattr(voice_engine_worker, "voice_engine_leadership", lambda: nullcontext(leader))
    monkeypatch.setattr(voice_engine_worker, "_expire_crashed_calls", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "heartbeat", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_native_test", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_transfer_test", lambda: None)
    monkeypatch.setattr(voice_engine_worker, "_claim_recipient", lambda: 9)

    def finish(_recipient_id, _leader):
        assert _leader is leader
        calls.append("completed")
        stop.requested = True

    monkeypatch.setattr(voice_engine_worker, "_run_campaign_call", finish)
    voice_engine_worker.main()
    assert calls == ["completed"]


def test_voice_test_worker_finishes_claimed_test(monkeypatch):
    stop = FakeStop()
    calls = []
    monkeypatch.setenv("VOICE_TEST_WORKER_ENABLED", "true")
    monkeypatch.setattr(voice_test_worker, "GracefulShutdown", lambda logger: stop)
    monkeypatch.setattr(voice_test_worker, "heartbeat", lambda: None)
    monkeypatch.setattr(voice_test_worker, "expire_stale", lambda: None)
    monkeypatch.setattr(voice_test_worker, "claim_next", lambda: (5, 6, "SIP_ONLY"))

    def finish(*_args):
        calls.append("completed")
        stop.requested = True

    monkeypatch.setattr(voice_test_worker, "run_one", finish)
    voice_test_worker.main()
    assert calls == ["completed"]


def test_real_sigterm_is_handled_in_subprocess():
    """Prova que o processo coopera com SIGTERM, sem serviços externos."""
    if os.name != "posix":
        return
    script = textwrap.dedent(
        """
        import logging
        from app.workers.graceful_shutdown import GracefulShutdown

        stop = GracefulShutdown(logging.getLogger("subprocess-test"))
        stop.install()
        print("READY", flush=True)
        while not stop.wait(0.05):
            pass
        print("STOPPED_GRACEFULLY", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 0, stderr
        assert "STOPPED_GRACEFULLY" in stdout
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)
