"""Testes de regressao do Prospecta VOZ sem realizar chamadas SIP."""
from __future__ import annotations

import re
import struct
import wave
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Lead, VoiceCampaign, VoiceRecipient, VoiceSipAccount
from app.services.voice import VOICE_STATUS_HOMOLOGACAO, normalize_voice_contacts, validate_campaign, validate_sip, validate_wav


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice").text)


def _wav() -> bytes:
    with BytesIO() as stream:
        with wave.open(stream, "wb") as player:
            player.setnchannels(1)
            player.setsampwidth(2)
            player.setframerate(8000)
            player.writeframes(struct.pack("<h", 0) * 8000)
        return stream.getvalue()


def test_sip_validation_and_audio_modes():
    assert validate_sip("SIP.Example.COM", "200", "udp") == ("sip.example.com", "200", "UDP")
    with pytest.raises(ValueError):
        validate_sip("https://unsafe.invalid", "200", "UDP")
    assert validate_campaign("DIRECT", "09:00", "18:00", "1", "") == "DIRECT"
    with pytest.raises(ValueError):
        validate_campaign("AUDIO", "09:00", "18:00", "1", "")
    validate_wav(_wav())
    with pytest.raises(ValueError):
        validate_wav(b"not-a-wave")


def test_voice_campaign_status_fits_production_column():
    """Guard against PostgreSQL varchar truncation in campaign review."""
    assert len(VOICE_STATUS_HOMOLOGACAO) <= VoiceCampaign.__table__.columns.status.type.length


def test_blocked_and_duplicate_contacts():
    rows = [
        {"phone": "55 55 99911-1000", "name": "Teste"},
        {"phone": "5555999111000", "name": "Repetido"},
        {"phone": "5555999222000", "name": "Bloqueado"},
    ]
    valid, skipped = normalize_voice_contacts(rows, {"5555999222000"})
    assert len(valid) == 1 and skipped == 2
    assert valid[0]["phone"] == "5555999111000"


def test_voice_pages_auth_csrf_and_drafts():
    with TestClient(app) as client:
        assert client.get("/voice", follow_redirects=False).status_code == 303
        csrf = _login(client)
        assert "Prospecta VOZ" in client.get("/voice").text
        invalid = client.post("/voice/accounts", data={
            "csrf_token": "invalid", "name": "Teste", "domain": "sip.example.com",
            "username": "200", "password": "dummy-password",
        }, follow_redirects=False)
        assert invalid.status_code == 403
        response = client.post("/voice/accounts", data={
            "csrf_token": csrf, "name": "Ramal de teste", "domain": "sip.example.com",
            "username": "200", "password": "dummy-password", "transport": "UDP",
        }, follow_redirects=False)
        assert response.status_code == 303
        response = client.post("/voice/groups", data={
            "csrf_token": csrf, "name": "Grupo teste", "target_extension": "700",
            "member_extensions": "201,202",
        }, follow_redirects=False)
        assert response.status_code == 303

        db = SessionLocal()
        try:
            account = db.scalar(select(VoiceSipAccount).where(VoiceSipAccount.username == "200"))
            assert account and account.password_encrypted != "dummy-password"
            account_id = account.id
            from app.models import VoiceGroup
            group_id = db.scalar(select(VoiceGroup.id).where(VoiceGroup.target_extension == "700"))
        finally:
            db.close()

        for mode in ("DIRECT", "AUDIO"):
            response = client.post("/voice/campaigns", data={
                "csrf_token": csrf, "name": "Teste " + mode, "sip_account_id": account_id,
                "group_id": group_id, "mode": mode, "window_start": "09:00",
                "window_end": "18:00", "daily_limit": 10, "interval_seconds": 30,
            }, follow_redirects=False)
            assert response.status_code == 303
        db = SessionLocal()
        try:
            campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.name == "Teste DIRECT"))
            assert campaign and campaign.status == "RASCUNHO"
            campaign_id = campaign.id
            # Bloqueio DNC existe antes da importacao
            db.add(Lead(business_name="Contato Bloqueado", phone="5555999222000",
                        do_not_contact=True, city="Santa Rosa"))
            db.commit()
        finally:
            db.close()

        response = client.post(f"/voice/campaigns/{campaign_id}/contacts", data={"csrf_token": csrf},
            files={"csv_file": ("contatos.csv",
                "nome;telefone\nDisponivel;5555999111000\nDuplicado;5555999111000\nBloqueado;5555999222000".encode(),
                "text/csv")},
            follow_redirects=False)
        assert response.status_code == 303
        db = SessionLocal()
        try:
            rows = list(db.scalars(select(VoiceRecipient).where(VoiceRecipient.campaign_id == campaign_id)))
            assert len(rows) == 1 and rows[0].status == "PENDENTE"
        finally:
            db.close()
        response = client.post(f"/voice/campaigns/{campaign_id}/review",
                               data={"csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert "PREPARADA" in client.get("/voice/campaigns").text
        db = SessionLocal()
        try:
            assert db.get(VoiceCampaign, campaign_id).status == VOICE_STATUS_HOMOLOGACAO
        finally:
            db.close()


def test_voice_audio_accepts_more_than_2mb_and_more_than_60_seconds():
    """Audio de campanha nao deve ser truncado por limite antigo do discador."""
    with BytesIO() as stream:
        with wave.open(stream, "wb") as player:
            player.setnchannels(1)
            player.setsampwidth(2)
            player.setframerate(16000)
            player.writeframes(b"\x00\x00" * (16000 * 65))
        data = stream.getvalue()
    assert len(data) > 2_000_000
    validate_wav(data)
    with pytest.raises(ValueError, match="capacidade"):
        validate_wav(data, max_bytes=2_000_000)
