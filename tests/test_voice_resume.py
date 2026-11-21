"""Regressoes: continuar uma campanha pausada nao e uma nova rodada de chamadas."""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog, VoiceCampaign, VoiceDialerEngine, VoiceGroup,
    VoiceRecipient, VoiceSipAccount,
)


def _csrf(html: str) -> str:
    result = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert result
    return result.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    result = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token
    }, follow_redirects=False)
    assert result.status_code == 303
    return _csrf(client.get("/voice/campaigns").text)


@contextmanager
def _paused_campaign(*, pending: bool = True, in_flight: bool = False,
                     started: bool = True, engine_enabled: bool = True):
    key = uuid4().hex[:9]
    moment = datetime.now(timezone.utc)
    with SessionLocal() as db:
        engine = db.get(VoiceDialerEngine, 1)
        created_engine = engine is None
        if engine is None:
            engine = VoiceDialerEngine(id=1, enabled=False, campaign_runtime_enabled=False,
                                       test_runtime_enabled=False, backend="PJSUA2",
                                       worker_id="", last_error="")
            db.add(engine)
            db.flush()
        previous = (engine.enabled, engine.campaign_runtime_enabled, engine.heartbeat_at)
        engine.enabled = engine_enabled
        engine.campaign_runtime_enabled = True
        engine.heartbeat_at = moment

        account = VoiceSipAccount(name="Conta resume " + key, username="2" + key[:4],
                                  domain="sip.example.com", transport="UDP",
                                  password_encrypted="placeholder", active=True)
        group = VoiceGroup(name="Grupo resume " + key, target_extension="300", active=True)
        db.add_all([account, group])
        db.flush()
        campaign = VoiceCampaign(
            name="Resume " + key, mode="DIRECT", status="PAUSADA",
            sip_account_id=account.id, group_id=group.id,
            window_start="09:00", window_end="18:00", daily_limit=1000,
            interval_seconds=60, dtmf_digit="1", legal_basis_verified=True,
            started_at=moment - timedelta(hours=2) if started else None,
            stopped_at=moment - timedelta(minutes=1),
            last_call_at=moment - timedelta(minutes=10),
        )
        db.add(campaign)
        db.flush()
        suffix = str(int(key, 16) % 1_000_000).zfill(6)
        entries = [
            VoiceRecipient(campaign_id=campaign.id, name="Ja discado",
                           phone="5555999" + suffix, status="CHAMANDO" if in_flight else "SEM_ATENDIMENTO",
                           attempts=1, last_attempt_at=moment - timedelta(minutes=10)),
            VoiceRecipient(campaign_id=campaign.id, name="Ainda pendente",
                           phone="5555989" + suffix, status="PENDENTE" if pending else "FALHOU",
                           attempts=0 if pending else 1,
                           last_attempt_at=None if pending else moment - timedelta(minutes=8)),
        ]
        db.add_all(entries)
        db.flush()
        identifiers = {
            "campaign": campaign.id, "account": account.id, "group": group.id,
            "recipients": [r.id for r in entries],
        }
        db.commit()
    try:
        yield identifiers
    finally:
        with SessionLocal() as db:
            db.execute(delete(AuditLog).where(
                AuditLog.entity_type == "voice_campaign",
                AuditLog.entity_id == str(identifiers["campaign"]),
            ))
            db.execute(delete(VoiceRecipient).where(
                VoiceRecipient.campaign_id == identifiers["campaign"]
            ))
            db.execute(delete(VoiceCampaign).where(VoiceCampaign.id == identifiers["campaign"]))
            db.execute(delete(VoiceGroup).where(VoiceGroup.id == identifiers["group"]))
            db.execute(delete(VoiceSipAccount).where(VoiceSipAccount.id == identifiers["account"]))
            engine = db.get(VoiceDialerEngine, 1)
            if created_engine:
                db.delete(engine)
            else:
                engine.enabled, engine.campaign_runtime_enabled, engine.heartbeat_at = previous
            db.commit()


def test_paused_campaign_shows_resume_separate_from_resend_and_preserves_history():
    with TestClient(app) as client:
        token = _login(client)
        with _paused_campaign() as case:
            cid = case["campaign"]
            detail = client.get(f"/voice/campaigns/{cid}/details")
            assert detail.status_code == 200
            assert f'action="/voice/campaigns/{cid}/resume"' in detail.text
            assert "Continuar de onde parou" in detail.text
            assert "Reenviar campanha inteira" in detail.text
            assert "Reenviar somente não atendidos" in detail.text
            assert "Iniciar campanha" not in detail.text

            with SessionLocal() as db:
                before = db.get(VoiceCampaign, cid)
                dates = (before.started_at, before.last_call_at)
                recipients = list(db.scalars(select(VoiceRecipient).where(
                    VoiceRecipient.campaign_id == cid).order_by(VoiceRecipient.id)))
                original = [(r.id, r.status, r.attempts, r.last_attempt_at) for r in recipients]
                campaign_count = db.scalar(select(func.count(VoiceCampaign.id)))

            response = client.post(f"/voice/campaigns/{cid}/resume", data={"csrf_token": token},
                                   follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].endswith(f"/voice/campaigns?open={cid}")
            with SessionLocal() as db:
                campaign = db.get(VoiceCampaign, cid)
                assert campaign.status == "EM_EXECUCAO"
                assert campaign.started_at == dates[0]
                assert campaign.last_call_at == dates[1]
                assert campaign.stopped_at is None
                now_rows = list(db.scalars(select(VoiceRecipient).where(
                    VoiceRecipient.campaign_id == cid).order_by(VoiceRecipient.id)))
                assert [(r.id, r.status, r.attempts, r.last_attempt_at) for r in now_rows] == original
                assert db.scalar(select(func.count(VoiceCampaign.id))) == campaign_count
                assert db.scalar(select(func.count(AuditLog.id)).where(
                    AuditLog.entity_type == "voice_campaign",
                    AuditLog.entity_id == str(cid),
                    AuditLog.action == "VOICE_CAMPAIGN_RESUMED",
                )) == 1

            # A second click cannot create another execution or reset contacts.
            duplicate = client.post(f"/voice/campaigns/{cid}/resume",
                                    data={"csrf_token": token}, follow_redirects=False)
            assert duplicate.status_code == 303
            with SessionLocal() as db:
                assert db.scalar(select(func.count(AuditLog.id)).where(
                    AuditLog.entity_type == "voice_campaign",
                    AuditLog.entity_id == str(cid),
                    AuditLog.action == "VOICE_CAMPAIGN_RESUMED",
                )) == 1


def test_resume_requires_csrf_admin_existing_campaign_and_pending_contacts():
    with TestClient(app) as client:
        with _paused_campaign() as case:
            cid = case["campaign"]
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": "wrong"}, follow_redirects=False).status_code == 303
            token = _login(client)
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": "wrong"}, follow_redirects=False).status_code == 403

        with _paused_campaign(pending=False) as case:
            cid = case["campaign"]
            html = client.get(f"/voice/campaigns/{cid}/details").text
            assert "Nenhum" not in html or "Nao ha contatos pendentes" in html
            assert re.search(r'type="submit" disabled[^>]*>▶ Continuar de onde parou', html)
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": token}, follow_redirects=False).status_code == 303
            with SessionLocal() as db:
                assert db.get(VoiceCampaign, cid).status == "PAUSADA"

        with _paused_campaign(started=False) as case:
            cid = case["campaign"]
            html = client.get(f"/voice/campaigns/{cid}/details").text
            assert f'action="/voice/campaigns/{cid}/resume"' not in html
            assert "Iniciar campanha" in html
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": token}, follow_redirects=False).status_code == 303
            with SessionLocal() as db:
                assert db.get(VoiceCampaign, cid).status == "PAUSADA"


def test_resume_rejects_inflight_calls_offline_engine_and_legacy_start():
    with TestClient(app) as client:
        token = _login(client)
        with _paused_campaign(in_flight=True) as case:
            cid = case["campaign"]
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": token}, follow_redirects=False).status_code == 303
            with SessionLocal() as db:
                assert db.get(VoiceCampaign, cid).status == "PAUSADA"

        with _paused_campaign(engine_enabled=False) as case:
            cid = case["campaign"]
            assert client.post(f"/voice/campaigns/{cid}/resume",
                               data={"csrf_token": token}, follow_redirects=False).status_code == 303
            assert client.post(f"/voice/campaigns/{cid}/start",
                               data={"csrf_token": token, "authorization": "sim"},
                               follow_redirects=False).status_code == 303
            with SessionLocal() as db:
                assert db.get(VoiceCampaign, cid).status == "PAUSADA"
                assert db.scalar(select(func.count(AuditLog.id)).where(
                    AuditLog.entity_type == "voice_campaign",
                    AuditLog.entity_id == str(cid),
                    AuditLog.action == "VOICE_CAMPAIGN_RESUME_BLOCKED",
                )) == 1
