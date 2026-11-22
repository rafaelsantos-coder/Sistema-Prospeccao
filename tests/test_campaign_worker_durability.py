"""Provas locais: envio depois da reserva duravel e parada segura, sem Z-API real."""
from __future__ import annotations

import logging
import signal
from types import SimpleNamespace

from app.workers import campaign_worker
from app.workers.graceful_shutdown import GracefulShutdown


class FakeStop:
    requested = False

    def install(self):
        pass

    def wait(self, _seconds):
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


def test_sigterm_sets_stop_flag_without_throwing(monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    stop = GracefulShutdown(logging.getLogger("test-worker-shutdown"))
    stop.install()
    assert signal.SIGTERM in handlers
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.requested
    assert stop.wait(0)


def test_shutdown_during_manual_outbox_never_claims_campaign(monkeypatch):
    stop = FakeStop()
    monkeypatch.setenv("CAMPAIGN_DISPATCH_PAUSED", "false")
    monkeypatch.setattr(campaign_worker, "GracefulShutdown", lambda _logger: stop)

    def manual():
        stop.requested = True
        return "sent"

    monkeypatch.setattr(campaign_worker, "process_one_pending", manual)
    monkeypatch.setattr(
        campaign_worker, "claim_next_dispatch_recipient",
        lambda _db: (_ for _ in ()).throw(AssertionError("Nao iniciar campanha")),
    )
    campaign_worker.run()
    assert stop.requested


def test_sigterm_after_claim_before_commit_rolls_back_and_skips_send(monkeypatch):
    stop, db = FakeStop(), FakeDb()
    monkeypatch.setenv("CAMPAIGN_DISPATCH_PAUSED", "false")
    monkeypatch.setattr(campaign_worker, "GracefulShutdown", lambda _logger: stop)
    monkeypatch.setattr(campaign_worker, "process_one_pending", lambda: "idle")
    monkeypatch.setattr(campaign_worker, "SessionLocal", lambda: db)

    def claim(_db):
        stop.requested = True
        return SimpleNamespace(id=1, dispatch_id=2, attempts=1)

    monkeypatch.setattr(campaign_worker, "claim_next_dispatch_recipient", claim)
    monkeypatch.setattr(
        campaign_worker, "process_dispatch_recipient",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Nao enviar no SIGTERM")),
    )
    campaign_worker.run()
    assert db.rollbacks == 1
    assert db.commits == 0
    assert db.closes == 1


def test_worker_commits_claim_before_simulated_provider_send(monkeypatch):
    stop, db = FakeStop(), FakeDb()
    monkeypatch.setenv("CAMPAIGN_DISPATCH_PAUSED", "false")
    monkeypatch.setattr(campaign_worker, "GracefulShutdown", lambda _logger: stop)
    monkeypatch.setattr(campaign_worker, "process_one_pending", lambda: "idle")
    monkeypatch.setattr(campaign_worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        campaign_worker, "claim_next_dispatch_recipient",
        lambda _db: SimpleNamespace(id=7, dispatch_id=8, attempts=1),
    )

    def fake_provider(_db, _recipient):
        assert db.commits == 1, "Sem claim durable antes do efeito externo"
        stop.requested = True
        return "sent"

    monkeypatch.setattr(campaign_worker, "process_dispatch_recipient", fake_provider)
    campaign_worker.run()
    assert db.commits == 2
    assert db.rollbacks == 0
    assert db.closes == 1
