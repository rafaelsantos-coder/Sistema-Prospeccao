"""Rotas Native: cinco origens, quatro mascaras, rodizio e zero chamadas reais."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, VoiceCampaign, VoiceRecipient, VoiceSipAccount
from app.services.voice_native_routing import (
    NATIVE_ORIGIN_NUMBERS, NativeRoutingError,
    native_dial_digits, selected_native_prefix, validate_native_campaign,
)
from app.services.voice_runtime import make_dial_uri, next_attempt_allowed


def test_native_four_masks_for_all_five_origin_numbers():
    # DDD 55, fixo e celular, com e sem DDI 55.
    for prefix in NATIVE_ORIGIN_NUMBERS:
        assert native_dial_digits("5535118003", prefix) == prefix + "35118003"
        assert native_dial_digits("555535118003", prefix) == prefix + "35118003"
        assert native_dial_digits("55999912345", prefix) == prefix + "9999912345"
        assert native_dial_digits("5555999912345", prefix) == prefix + "9999912345"
        # DDD 54, fixo e celular.
        assert native_dial_digits("5435118003", prefix) == prefix + "05435118003"
        assert native_dial_digits("555435118003", prefix) == prefix + "05435118003"
        assert native_dial_digits("54999912345", prefix) == prefix + "0549999912345"
        assert native_dial_digits("5554999912345", prefix) == prefix + "0549999912345"


def test_native_masks_fail_closed_for_invalid_destinations():
    for number in ("5591111234", "5581234567", "5598888777", "0800123456"):
        with pytest.raises(ValueError):
            native_dial_digits(number, "8003")
    with pytest.raises(NativeRoutingError, match="Techprefix"):
        native_dial_digits("5555999912345", "8008")
    with pytest.raises(NativeRoutingError, match="DDD"):
        native_dial_digits("5555999912345", "8003", "00")


def test_native_prefix_replaces_not_appends_legacy_account_prefix():
    acc = VoiceSipAccount(domain="pbx.example.com", username="1804",
                          dial_prefix="99", sip_port=5060, transport="UDP")
    result = make_dial_uri(acc, "5554999912345",
                           dial_format="E164", native_techprefix="8005",
                           native_local_ddd="55")
    assert result == "sip:8005054999912345@pbx.example.com:5060;transport=udp"
    assert "998005" not in result
    # Fora do modo Native, preserva o comportamento de todos os clientes legados.
    assert make_dial_uri(acc, "5554999912345", dial_format="NATIONAL").startswith("sip:9954999912345@")


def test_native_routing_rejects_unapproved_account_prefix_or_empty_lines():
    with pytest.raises(NativeRoutingError, match="1804"):
        validate_native_campaign("NATIVE_ROUND_ROBIN", ["8003"], "8003", "55", "200")
    with pytest.raises(NativeRoutingError, match="pelo menos"):
        validate_native_campaign("NATIVE_ROUND_ROBIN", [], "8003", "55", "1804")
    with pytest.raises(NativeRoutingError, match="repetido"):
        validate_native_campaign("NATIVE_ROUND_ROBIN", ["8003", "8003"], "8003", "55", "1804")
    with pytest.raises(NativeRoutingError, match="fora"):
        validate_native_campaign("NATIVE_ROUND_ROBIN", ["8003", "8011"], "8003", "55", "1804")
    assert validate_native_campaign(
        "NATIVE_ROUND_ROBIN", ["8007", "8003", "8005"], "8004", "55", "1804"
    ) == ("NATIVE_ROUND_ROBIN", "8003,8005,8007", "55")
    assert validate_native_campaign(
        "NATIVE_FIXED", ["8003", "8004"], "8006", "55", "1804"
    ) == ("NATIVE_FIXED", "8006", "55")
    assert validate_native_campaign(
        "ACCOUNT", [], "8003", "55", "200"
    ) == ("ACCOUNT", "", "55")


def test_round_robin_is_deterministic_after_pause_and_worker_restart():
    campaign = VoiceCampaign(
        outbound_routing_mode="NATIVE_ROUND_ROBIN",
        native_techprefixes="8003,8004,8005,8006,8007",
    )
    expected = ["8003", "8004", "8005", "8006", "8007", "8003", "8004"]
    assert [selected_native_prefix(campaign, count) for count in range(7)] == expected
    # Counts are persisted as VoiceRecipient.outbound_techprefix rows.
    assert selected_native_prefix(campaign, 3) == "8006"
    campaign.status = "PAUSADA"
    campaign.status = "EM_EXECUCAO"
    assert selected_native_prefix(campaign, 3) == "8006"
    campaign.outbound_routing_mode = "NATIVE_FIXED"
    campaign.native_techprefixes = "8007"
    assert all(selected_native_prefix(campaign, n) == "8007" for n in range(6))
    campaign.outbound_routing_mode = "ACCOUNT"
    assert selected_native_prefix(campaign, 30) is None


def test_no_hidden_daily_cap_of_100(monkeypatch):
    import app.services.voice_runtime as runtime
    at = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
    camp = VoiceCampaign(
        window_start="08:00", window_end="20:00",
        daily_limit=1000, interval_seconds=10, last_call_at=at - timedelta(seconds=15),
    )
    monkeypatch.setattr(runtime, "campaign_daily_count", lambda *_args: 150)
    assert next_attempt_allowed(None, camp, at)
    camp.daily_limit = 150
    assert not next_attempt_allowed(None, camp, at)


def _token(body: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert found
    return found.group(1)


def test_admin_creates_native_campaign_with_selected_lines_and_preview():
    with TestClient(app) as client:
        login_token = _token(client.get("/login").text)
        auth = client.post("/login", data={
            "email": "admin@sulnet.local", "password": "Sulnet@2026!",
            "csrf_token": login_token,
        }, follow_redirects=False)
        assert auth.status_code == 303
        csrf = _token(client.get("/voice/campaigns").text)
        account_name = "Native 1804 " + uuid4().hex[:10]
        campaign_name = "Teste Rodizio Native " + uuid4().hex[:9]
        with SessionLocal() as db:
            acc = VoiceSipAccount(name=account_name, domain="sip.example.com",
                                  username="1804", password_encrypted="fake-for-ci",
                                  transport="UDP", active=True)
            db.add(acc)
            db.commit()
            aid = acc.id
        try:
            page = client.get("/voice/campaigns/new")
            assert page.status_code == 200
            assert "NATIVE_ROUND_ROBIN" in page.text
            assert "8003" in page.text and "8007" in page.text

            created = client.post("/voice/campaigns", data={
                "csrf_token": csrf, "name": campaign_name,
                "sip_account_id": str(aid), "group_id": "",
                "mode": "AUDIO", "dtmf_digit": "1",
                "dial_format": "NATIONAL", "window_start": "09:00",
                "window_end": "18:00", "daily_limit": "1000",
                "interval_seconds": "10",
                "outbound_routing_mode": "NATIVE_ROUND_ROBIN",
                "native_techprefixes": ["8007", "8004", "8003"],
                "native_local_ddd": "55",
            }, follow_redirects=False)
            assert created.status_code == 303
            with SessionLocal() as db:
                campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.name == campaign_name))
                assert campaign is not None
                cid = campaign.id
                assert campaign.daily_limit == 1000
                assert campaign.outbound_routing_mode == "NATIVE_ROUND_ROBIN"
                assert campaign.native_techprefixes == "8003,8004,8007"
                db.add(VoiceRecipient(
                    campaign_id=cid, name="Contato teste",
                    phone="5555999912345", status="PENDENTE", attempts=0,
                ))
                db.commit()
            details = client.get(f"/voice/campaigns/{cid}/details")
            assert details.status_code == 200
            assert "8003999912345" in details.text
            assert "Rodizio automatico" in details.text
            assert "Native" in details.text
            edit = client.get(f"/voice/campaigns/{cid}/edit")
            assert edit.status_code == 200
            assert "NATIVE_ROUND_ROBIN" in edit.text
            assert "native_fixed_prefix" in edit.text
        finally:
            from sqlalchemy import delete
            with SessionLocal() as db:
                camp = db.scalar(select(VoiceCampaign).where(VoiceCampaign.name == campaign_name))
                if camp:
                    db.execute(delete(VoiceRecipient).where(VoiceRecipient.campaign_id == camp.id))
                    db.execute(delete(AuditLog).where(
                        AuditLog.entity_type == "voice_campaign",
                        AuditLog.entity_id == str(camp.id),
                    ))
                    db.delete(camp)
                account = db.get(VoiceSipAccount, aid)
                if account:
                    db.delete(account)
                db.commit()
