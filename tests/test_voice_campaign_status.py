"""Status real de campanhas: tentativas, bloqueios e autorizacao de administrador."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog, VoiceCallEvent, VoiceCampaign, VoiceGroup,
    VoiceRecipient, VoiceSipAccount,
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local",
        "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice").text)


def test_voice_campaign_tracking_and_denied_start_are_persisted():
    with TestClient(app) as client:
        assert client.get("/voice/campaigns/status", follow_redirects=False).status_code == 303
        token = _login(client)
        with SessionLocal() as db:
            account = VoiceSipAccount(
                name="Conta status", username="9988", domain="sip.example.com",
                password_encrypted="placeholder", active=True, transport="UDP",
            )
            group = VoiceGroup(
                name="Grupo status", target_extension="300", active=True,
            )
            db.add_all([account, group])
            db.flush()
            campaign = VoiceCampaign(
                name="Teste status real", mode="DIRECT", sip_account_id=account.id,
                group_id=group.id, status="RASCUNHO", legal_basis_verified=False,
                window_start="09:00", window_end="18:00",
            )
            db.add(campaign)
            db.flush()
            pending = VoiceRecipient(
                campaign_id=campaign.id, name="Contato pendente",
                phone="5555999111100", status="PENDENTE", attempts=0,
            )
            failed = VoiceRecipient(
                campaign_id=campaign.id, name="Contato teste",
                phone="5555999222200", status="FALHOU", attempts=1,
                last_attempt_at=datetime.now(timezone.utc),
                last_error="Timeout SIP",
            )
            db.add_all([pending, failed])
            db.flush()
            db.add(VoiceCallEvent(
                recipient_id=failed.id, event_id=uuid4().hex,
                event="FALHOU", detail="Timeout SIP",
            ))
            db.commit()
            campaign_id = campaign.id

        response = client.get("/voice/campaigns/status")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()["campaigns"][str(campaign_id)]
        assert data["status"] == "RASCUNHO"
        assert data["stats"]["total"] == 2
        assert data["stats"]["attempted"] == 1
        assert data["stats"]["pending"] == 1
        assert data["stats"]["failed"] == 1
        assert data["stats"]["answered"] == 0
        assert data["stats"]["transferred"] == 0
        assert data["last_attempts"][0]["phone_suffix"] == "2200"
        assert "Timeout SIP" in data["events"][0]["detail"]
        assert "Não iniciada" in data["headline"]
        assert "Nenhuma solicitação" in data["last_command"]["label"]

        page = client.get("/voice/campaigns").text
        assert "ACOMPANHAMENTO DA CAMPANHA" in page
        assert "Solicitar início da campanha" in page
        assert "voice_campaign_status.js" in page
        assert 'data-voice-stat="attempted"' in page

        response = client.post(
            f"/voice/campaigns/{campaign_id}/start",
            data={"csrf_token": token, "authorization": "sim"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        data = client.get("/voice/campaigns/status").json()["campaigns"][str(campaign_id)]
        assert data["status"] == "RASCUNHO"
        assert data["last_command"]["label"] == "Tentativa de início bloqueada"
        assert data["last_command"]["detail"]
        with SessionLocal() as db:
            assert db.scalar(select(AuditLog).where(
                AuditLog.entity_type == "voice_campaign",
                AuditLog.entity_id == str(campaign_id),
                AuditLog.action == "VOICE_CAMPAIGN_START_BLOCKED",
            )) is not None
            assert db.get(VoiceCampaign, campaign_id).started_at is None
