"""Prospecta VOZ readiness, opt-out, manual authorization, routing and kill-switch."""
from __future__ import annotations

import re
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    GlobalContactBlacklist, VoiceCampaign, VoiceDialerEngine, VoiceGroup,
    VoiceRecipient, VoiceSipAccount, VoiceTransferTest,
)
from app.services.secret_store import encrypt_secret
from app.services.voice_pjsua2 import PjSession, VoiceEngineError
from app.services.voice_runtime import (
    VoiceReadinessError, check_campaign_ready, make_dial_uri, make_transfer_uri,
    next_attempt_allowed, outbound_guard, validate_routing_options, within_call_window,
)


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    csrf = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "csrf_token": csrf, "email": "admin@sulnet.local", "password": "Sulnet@2026!",
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice").text)


def _scenario():
    key = uuid4().hex[:8]
    moment = datetime.now(timezone.utc)
    with SessionLocal() as db:
        account = VoiceSipAccount(name=f"Native {key}", username=f"880{key[:4]}",
            domain="sip.example.com", transport="UDP", sip_port=5070,
            proxy_host="192.0.2.10", proxy_port=5071,
            dial_domain="pbx.example.com", dial_prefix="0", strip_brazil_ddi=True,
            password_encrypted=encrypt_secret("not-a-real-password"),
            active=True, sip_check_at=moment, sip_check_code=200)
        group = VoiceGroup(name=f"Team {key}", target_extension="700", active=True,
                           transfer_verified_at=moment)
        db.add_all([account, group])
        db.flush()
        campaign = VoiceCampaign(name=f"Campaign {key}", mode="DIRECT",
            sip_account_id=account.id, group_id=group.id, status="EM_HOMOLOGACAO",
            dtmf_digit="1", daily_limit=10, interval_seconds=60,
            window_start="09:00", window_end="18:00",
            legal_basis_verified=False, audio_uri="")
        db.add(campaign)
        db.flush()
        db.add(VoiceRecipient(
            campaign_id=campaign.id, name="Teste", phone="5555999980100",
            status="PENDENTE", attempts=0,
        ))
        db.commit()
        return account.id, group.id, campaign.id


def test_sip_advanced_options_and_normalized_dial_uri():
    config = validate_routing_options(
        5070, "192.0.2.10", 5071, "PBX.EXAMPLE.COM", "0", "555199999999"
    )
    assert config["sip_port"] == 5070
    assert config["proxy_host"] == "192.0.2.10"
    assert config["dial_domain"] == "pbx.example.com"
    assert config["dial_prefix"] == "0"
    for kwargs in ({"sip_port": 0}, {"proxy_port": 65536},
                   {"proxy_host": "https://bad.host/path"}, {"dial_prefix": "+9"}):
        with pytest.raises(ValueError):
            validate_routing_options(**kwargs)
    a = VoiceSipAccount(domain="sip.example.com", username="200",
                        sip_port=5070, dial_domain="pbx.example.com", dial_prefix="0",
                        strip_brazil_ddi=True, transport="UDP")
    g = VoiceGroup(name="Comercial", target_extension="700", active=True)
    assert make_dial_uri(a, "5555999991000") == "sip:055999991000@pbx.example.com:5070;transport=udp"
    assert make_transfer_uri(a, g) == "sip:700@pbx.example.com:5070;transport=udp"


def test_blacklist_fail_closed_for_all_voice_channels(monkeypatch):
    with SessionLocal() as db:
        row = GlobalContactBlacklist(phone="5555999980200", active=True)
        db.add(row)
        db.commit()
        with pytest.raises(VoiceReadinessError, match="blacklist"):
            outbound_guard(db, "55 55 99998-0200")
    import app.services.voice_runtime as runtime
    def fail(_db, _phone):
        raise RuntimeError("db offline")
    monkeypatch.setattr(runtime, "is_globally_blocked", fail)
    with SessionLocal() as db, pytest.raises(VoiceReadinessError, match="suspensa"):
        outbound_guard(db, "5555999980300")


def test_call_schedule_conservative_weekdays():
    camp = VoiceCampaign(window_start="09:00", window_end="18:00",
                         daily_limit=10, interval_seconds=60)
    assert within_call_window(camp, datetime(2026, 11, 19, 13, 30, tzinfo=timezone.utc))
    assert not within_call_window(camp, datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc))
    assert not within_call_window(camp, datetime(2026, 11, 22, 13, 30, tzinfo=timezone.utc))


def test_no_campaign_start_until_account_group_pair_and_legal_cleared():
    with TestClient(app) as client:
        token = _login(client)
        aid, gid, cid = _scenario()
        with SessionLocal() as db:
            engine = db.get(VoiceDialerEngine, 1)
            assert engine is not None
            engine.enabled = False
            engine.campaign_runtime_enabled = False
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            campaign = db.get(VoiceCampaign, cid)
            reasons = check_campaign_ready(db, campaign)
            assert any("pausado" in x for x in reasons)
            assert any("base legal" in x.lower() for x in reasons)

        denied = client.post(f"/voice/campaigns/{cid}/start",
                             data={"csrf_token": token, "authorization": "sim"},
                             follow_redirects=False)
        assert denied.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceCampaign, cid).status == "EM_HOMOLOGACAO"

        accepted = client.post(f"/voice/campaigns/{cid}/legal",
             data={"csrf_token": token, "confirmation": "confirmado"},
             follow_redirects=False)
        assert accepted.status_code == 303
        with SessionLocal() as db:
            engine = db.get(VoiceDialerEngine, 1)
            engine.enabled = True
            engine.campaign_runtime_enabled = True
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            campaign = db.get(VoiceCampaign, cid)
            assert any("A conta desta campanha" in x for x in check_campaign_ready(db, campaign))

            db.add(VoiceTransferTest(account_id=aid, group_id=gid, phone_last4="0100",
                  result="TRANSFERIDA", sip_code=200, message="SIP REFER confirmado",
                  finished_at=datetime.now(timezone.utc)))
            db.commit()
            assert check_campaign_ready(db, campaign) == []
            campaign.last_call_at = datetime.now(timezone.utc)
            db.commit()
            # Even with approvals, the progressive worker applies a minimum delay.
            assert not next_attempt_allowed(db, campaign, datetime.now(timezone.utc))
            engine.enabled = False
            db.commit()


def test_admin_account_changes_reset_sip_and_transfer_certifications():
    with TestClient(app) as client:
        token = _login(client)
        aid, gid, _ = _scenario()
        with SessionLocal() as db:
            db.add(VoiceTransferTest(account_id=aid, group_id=gid, phone_last4="0000",
                result="TRANSFERIDA", sip_code=200, message="Earlier test"))
            db.commit()
        response = client.post(f"/voice/accounts/{aid}/update", data={
            "csrf_token": token, "name": "Atualizada", "domain": "pbx.example.com",
            "username": "202", "transport": "UDP",
            "sip_port": "5080", "proxy_host": "198.51.100.25",
            "proxy_port": "5081", "dial_domain": "", "dial_prefix": "0",
            "password": "",
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            acc = db.get(VoiceSipAccount, aid)
            assert acc.sip_port == 5080
            assert acc.proxy_host == "198.51.100.25"
            assert acc.sip_check_code is None
            assert all(x.result == "INVALIDADO" for x in db.scalars(select(
                VoiceTransferTest).where(VoiceTransferTest.account_id == aid)))


def test_public_dns_failure_prevents_native_call(monkeypatch):
    a = VoiceSipAccount(domain="sip.invalid", username="200", sip_port=5060, transport="UDP")
    def fail_dns(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    with pytest.raises(VoiceEngineError, match="DNS"):
        with PjSession(a, "fake"):
            pytest.fail("Native driver should not start if registrar has no DNS")


def test_emergency_stop_requires_admin_and_prevents_calls():
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            engine = db.get(VoiceDialerEngine, 1)
            engine.enabled = True
            engine.campaign_runtime_enabled = False
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
        response = client.post("/voice/engine/toggle", data={"csrf_token": token},
                               follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceDialerEngine, 1).enabled is False
