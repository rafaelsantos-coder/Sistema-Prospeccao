"""Exclusividade de conta SIP entre campanhas e testes, sem rede/INVITE."""
from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.services.voice_pjsua2 import VoiceEngineError
from app.services.voice_sip_probe import SipProbe, SipProbeError
from app.workers import sip_account_guard, voice_engine_worker, voice_test_worker
from app.workers.sip_account_guard import SIPAccountLockLost


PG_TEST_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/sulnet_ci"


def test_account_lock_rejects_sqlite():
    with pytest.raises(SIPAccountLockLost, match="PostgreSQL"):
        with sip_account_guard.sip_account_lock(1):
            pass


@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true",
    reason="Usa o PostgreSQL 18 descartável do GitHub Actions",
)
def test_same_sip_account_cannot_be_used_by_two_workers(monkeypatch):
    engine = create_engine(PG_TEST_URL)
    monkeypatch.setattr(sip_account_guard, "engine", engine)
    try:
        with sip_account_guard.sip_account_lock(101) as first:
            assert first is not None
            with sip_account_guard.sip_account_lock(101) as second:
                assert second is None
            # Contas diferentes podem ser testadas sem paralisar toda a operação.
            with sip_account_guard.sip_account_lock(102) as other:
                assert other is not None
            first.assert_held(force=True)
        with sip_account_guard.sip_account_lock(101) as new_owner:
            assert new_owner is not None
    finally:
        engine.dispose()


def test_voice_engine_rejects_busy_sip_account_without_creating_session(monkeypatch):
    account = SimpleNamespace(id=10)
    monkeypatch.setattr(voice_engine_worker, "sip_account_lock", lambda _id: nullcontext(None))
    monkeypatch.setattr(
        voice_engine_worker, "PjSession",
        lambda *_args: pytest.fail("Nao iniciar SIP em conta ocupada"),
    )
    fake_leader = SimpleNamespace(assert_held=lambda **_kwargs: None)
    with pytest.raises(VoiceEngineError, match="Conta SIP em uso"):
        with voice_engine_worker._protected_pj_session(account, "test", fake_leader):
            pytest.fail("Nao pode abrir chamada sem lock")


def test_voice_test_worker_rejects_busy_account_before_probe(monkeypatch):
    account = SimpleNamespace(
        id=22, active=True, transport="UDP", domain="sip.example.com",
        sip_port=5060, proxy_host="", proxy_port=5060, username="100",
        password_encrypted="fake-secret",
    )

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _model, _id):
            return account

    records = []
    monkeypatch.setattr(voice_test_worker, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(voice_test_worker, "decrypt_secret", lambda _token: "fake-password")
    monkeypatch.setattr(voice_test_worker, "sip_account_lock", lambda _id: nullcontext(None))
    monkeypatch.setattr(
        voice_test_worker, "SipProbe",
        lambda **_kwargs: pytest.fail("SIP de teste nao pode ser iniciado"),
    )
    monkeypatch.setattr(voice_test_worker, "_record", lambda *args: records.append(args))
    voice_test_worker.run_one(1, 22, "SIP_ONLY")
    assert len(records) == 1
    assert records[0][1] == "FALHOU"
    assert "ocupada" in records[0][4]


def test_sip_probe_aborts_before_invite_if_account_lost():
    probe = SipProbe(
        host="sip.example.com", username="100", password="fake",
        phone="5555999998874", notify=lambda *_args: None,
        should_abort=lambda: True,
    )
    with pytest.raises(SipProbeError, match="exclusividade"):
        probe._exchange(
            "INVITE", "sip:5555999998874@sip.example.com",
            "fake-call", "<sip:5555999998874@sip.example.com>",
        )
