"""Recover old audio/DTMF states, never place SIP calls or reset attempts."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import VoiceCallEvent, VoiceCampaign, VoiceRecipient, VoiceSipAccount, VoiceTestCall
from app.services.lia_voz_pilot import ensure_idle, PilotDenied
from app.workers import voice_engine_worker as worker
import pytest


def test_stale_audio_dtmf_recovery_requires_slot_preserves_history(monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    instant = datetime.now(timezone.utc)
    monkeypatch.setattr(worker, "now", lambda: instant)
    old = instant - timedelta(days=7)
    with sessions() as db:
        account = VoiceSipAccount(name="Test", domain="sip.example.com", username="1804",
                                  password_encrypted="fake", active=True)
        db.add(account)
        db.flush()
        campaign = VoiceCampaign(name="Old", sip_account_id=account.id, status="PAUSADA")
        db.add(campaign)
        db.flush()
        records = []
        for i, status in enumerate(["REPRODUZINDO_AUDIO", "CONFIRMADO_DTMF", "TOCANDO", "PENDENTE"]):
            row = VoiceRecipient(campaign_id=campaign.id, phone=f"55559999100{i:02d}",
                                 status=status, attempts=1 if i<3 else 0,
                                 last_attempt_at=old if i<2 else instant,
                                 dial_token=f"attempt-{i}" if i<3 else None)
            db.add(row)
            records.append(row)
        finished = VoiceTestCall(account_id=account.id, account_label="Test",
                                 target_phone="5555999910099", status="ATENDIDA",
                                 created_at=old, started_at=old, finished_at=old)
        db.add(finished)
        db.flush()
        ids = [r.id for r in records]
        finished_id = finished.id
        db.commit()
    monkeypatch.setattr(worker, "slot_healthy", lambda **kw: False)
    worker._expire_crashed_calls()
    with sessions() as db:
        assert db.get(VoiceRecipient, ids[0]).status == "REPRODUZINDO_AUDIO"
    monkeypatch.setattr(worker, "slot_healthy", lambda **kw: True)
    worker._expire_crashed_calls()
    worker._expire_crashed_calls()
    with sessions() as db:
        for i in range(2):
            row = db.get(VoiceRecipient, ids[i])
            assert row.status == "INDETERMINADO"
            assert row.attempts == 1
            assert row.dial_token == f"attempt-{i}"
        assert db.get(VoiceRecipient, ids[2]).status == "TOCANDO"
        assert db.get(VoiceRecipient, ids[3]).status == "PENDENTE"
        assert db.get(VoiceTestCall, finished_id).status == "ATENDIDA"
        events = list(db.scalars(select(VoiceCallEvent)))
        assert len(events) == 2
        assert all(e.event == "RECUPERACAO_ESTADO" for e in events)
        assert any("REPRODUZINDO_AUDIO" in e.detail for e in events)
        with pytest.raises(PilotDenied):
            ensure_idle(db, shared=True)
        db.get(VoiceRecipient, ids[2]).status = "FINALIZADA"
        db.commit()
        ensure_idle(db, shared=True)
    engine.dispose()
