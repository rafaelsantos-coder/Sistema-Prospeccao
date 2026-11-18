"""Plano de discagem SIP: distinguir DDI Brasil 55 do DDD regional 55."""
from __future__ import annotations

import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import VoiceCampaign, VoiceRecipient, VoiceSipAccount
from app.services.scoring import normalize_phone
from app.services.voice_runtime import VoiceReadinessError, make_dial_uri


def _token(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_ddi_55_not_prepended_to_ddd_55_in_national_campaign():
    account = VoiceSipAccount(
        name="PABX nacional", domain="pbx.example.com", username="200",
        sip_port=5060, transport="UDP", dial_prefix="",
        strip_brazil_ddi=False,
    )
    original_csv = "55999002508"  # DDD 55 + celular, sem DDI.
    canonical_crm = normalize_phone(original_csv)
    assert canonical_crm == "5555999002508"  # DDI 55 + DDD 55 + celular.

    uri = make_dial_uri(account, canonical_crm, dial_format="NATIONAL")
    assert uri == "sip:55999002508@pbx.example.com:5060;transport=udp"
    assert make_dial_uri(account, original_csv, dial_format="NATIONAL") == uri

    # Somente quando selecionado explicitamente para central que exige E.164:
    expected_e164 = "sip:5555999002508@pbx.example.com:5060;transport=udp"
    assert make_dial_uri(account, canonical_crm, dial_format="E164") == expected_e164
    assert make_dial_uri(account, original_csv, dial_format="E164") == expected_e164

    # Testes SIP avulsos seguem a configuracao da conta, sem mudar inesperadamente.
    assert make_dial_uri(account, canonical_crm) == expected_e164
    account.strip_brazil_ddi = True
    assert make_dial_uri(account, canonical_crm) == uri
    with pytest.raises(VoiceReadinessError):
        make_dial_uri(account, canonical_crm, dial_format="UNKNOWN")


def test_admin_can_create_national_voice_campaign_and_preview_real_sip_number():
    with TestClient(app) as client:
        login_csrf = _token(client.get("/login").text)
        login = client.post("/login", data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": login_csrf,
        }, follow_redirects=False)
        assert login.status_code == 303
        token = _token(client.get("/voice").text)

        with SessionLocal() as db:
            account = VoiceSipAccount(
                name="PABX campanha", domain="pbx.example.com",
                username="200", password_encrypted="test",
                sip_port=5060, transport="UDP", strip_brazil_ddi=False,
                active=True,
            )
            db.add(account)
            db.commit()
            account_id = account.id

        campaign_name = "DDD 55 " + uuid4().hex[:10]
        created = client.post("/voice/campaigns", data={
            "csrf_token": token,
            "name": campaign_name, "sip_account_id": account_id,
            "group_id": "", "mode": "AUDIO", "dtmf_digit": "1",
            "dial_format": "NATIONAL", "window_start": "09:00",
            "window_end": "18:00", "daily_limit": 10,
            "interval_seconds": 60,
        }, follow_redirects=False)
        assert created.status_code == 303

        with SessionLocal() as db:
            campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.name == campaign_name))
            assert campaign is not None
            assert campaign.dial_format == "NATIONAL"
            campaign_id = campaign.id
            db.add(VoiceRecipient(
                campaign_id=campaign.id, name="Teste regional",
                phone="5555999002508", status="PENDENTE",
            ))
            db.commit()

        page = client.get("/voice/campaigns")
        assert page.status_code == 200
        assert campaign_name in page.text
        assert "Próximo exemplo discado:" not in page.text
        details = client.get(f"/voice/campaigns/{campaign_id}/details")
        assert details.status_code == 200
        assert "Formato SIP:" in details.text
        assert "DDD + número (sem DDI)" in details.text
        assert "Próximo exemplo discado:" in details.text
        assert "55999002508" in details.text
        assert "5555999002508" not in details.text

        new_page = client.get("/voice/campaigns/new")
        assert new_page.status_code == 200
        assert 'value="NATIONAL" selected' in new_page.text
        assert 'value="E164"' in new_page.text
