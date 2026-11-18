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
    next_attempt_allowed, next_call_window_start, outbound_guard,
    validate_routing_options, within_call_window,
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


def _ensure_engine(db):
    # The pytest SQLite fixture creates metadata directly; Alembic seeds this
    # singleton in production, so tests must seed it explicitly.
    engine = db.get(VoiceDialerEngine, 1)
    if engine is None:
        engine = VoiceDialerEngine(
            id=1, enabled=False, campaign_runtime_enabled=False,
            test_runtime_enabled=False, backend="PJSUA2", worker_id="", last_error="",
        )
        db.add(engine)
        db.flush()
    return engine


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


def test_call_schedule_respects_campaign_window_including_weekends():
    camp = VoiceCampaign(window_start="09:00", window_end="20:00",
                         daily_limit=10, interval_seconds=60)
    assert within_call_window(camp, datetime(2026, 11, 19, 13, 30, tzinfo=timezone.utc))
    assert within_call_window(camp, datetime(2026, 11, 18, 21, 41, tzinfo=timezone.utc))
    assert not within_call_window(camp, datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc))
    assert within_call_window(camp, datetime(2026, 11, 22, 13, 30, tzinfo=timezone.utc))
    assert not within_call_window(camp, datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc))


def test_next_call_window_uses_only_campaign_schedule():
    campaign = VoiceCampaign(window_start="09:00", window_end="20:00")
    next_start = next_call_window_start(
        campaign, datetime(2026, 11, 18, 23, 14, tzinfo=timezone.utc))
    assert next_start is not None
    assert next_start.astimezone(timezone.utc) == datetime(
        2026, 11, 19, 12, 0, tzinfo=timezone.utc
    )
    saturday = next_call_window_start(
        campaign, datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc))
    assert saturday is not None
    assert saturday.astimezone(timezone.utc) == datetime(
        2026, 11, 21, 12, 0, tzinfo=timezone.utc
    )


def test_campaign_uses_registered_group_without_prior_refer_test():
    with TestClient(app) as client:
        token = _login(client)
        aid, gid, cid = _scenario()
        with SessionLocal() as db:
            engine = _ensure_engine(db)
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
            engine = _ensure_engine(db)
            engine.enabled = True
            engine.campaign_runtime_enabled = True
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            campaign = db.get(VoiceCampaign, cid)
            # Nenhuma homologacao de REFER foi realizada; o grupo cadastrado
            # sera usado diretamente na chamada real.
            group = db.get(VoiceGroup, gid)
            group.transfer_verified_at = None
            assert not db.scalars(select(VoiceTransferTest).where(
                VoiceTransferTest.account_id == aid,
                VoiceTransferTest.group_id == gid,
            )).first()
            db.commit()
            assert check_campaign_ready(db, campaign) == []
            account = db.get(VoiceSipAccount, aid)
            # O REGISTER anterior expira à noite; o worker deve fazer outro
            # antes do INVITE, sem bloquear a fila automaticamente.
            account.sip_check_at = datetime.now(timezone.utc) - timedelta(days=2)
            account.sip_check_code = 401
            db.commit()
            assert check_campaign_ready(db, campaign) == []
            group.active = False
            db.commit()
            assert any("Grupo de ramais ausente ou pausado" in x
                       for x in check_campaign_ready(db, campaign))
            group.active = True
            db.commit()
            assert check_campaign_ready(db, campaign) == []
        groups_page = client.get("/voice/groups")
        assert groups_page.status_code == 200
        assert "Teste real de transferência SIP REFER" not in groups_page.text
        assert "Transferência não validada" not in groups_page.text
        campaign_page = client.get("/voice/campaigns")
        assert campaign_page.status_code == 200
        assert "A transferência exige teste real de REFER" not in campaign_page.text

        started = client.post(f"/voice/campaigns/{cid}/start",
                              data={"csrf_token": token, "authorization": "sim"},
                              follow_redirects=False)
        assert started.status_code == 303
        with SessionLocal() as db:
            campaign = db.get(VoiceCampaign, cid)
            assert campaign.status == "EM_EXECUCAO"
            # O resultado REFER e da chamada real, e nao um prerequisito.
            campaign.last_call_at = datetime.now(timezone.utc)
            db.commit()
            assert not next_attempt_allowed(db, campaign, datetime.now(timezone.utc))

        paused = client.post(f"/voice/campaigns/{cid}/pause",
                             data={"csrf_token": token}, follow_redirects=False)
        assert paused.status_code == 303
        with SessionLocal() as db:
            engine = _ensure_engine(db)
            campaign = db.get(VoiceCampaign, cid)
            assert campaign.status == "PAUSADA"
            campaign.mode = "AUDIO"
            campaign.audio_uri = "s3://wave-placeholder/camp.wav"
            db.commit()
            assert check_campaign_ready(db, campaign) == []
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
            engine = _ensure_engine(db)
            engine.enabled = True
            engine.campaign_runtime_enabled = False
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
        response = client.post("/voice/engine/toggle", data={"csrf_token": token},
                               follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceDialerEngine, 1).enabled is False


def test_admin_can_queue_native_call_without_group_and_blacklist_applies():
    from app.models import VoiceTestCall
    with TestClient(app) as client:
        csrf = _login(client)
        aid, _gid, _cid = _scenario()
        with SessionLocal() as db:
            engine = _ensure_engine(db)
            engine.enabled = True
            engine.test_runtime_enabled = True
            engine.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
        response = client.post(f"/voice/accounts/{aid}/test-native", data={
            "csrf_token": csrf, "phone": "5555999970400", "authorized": "sim",
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            pending = list(db.scalars(select(VoiceTestCall).where(
                VoiceTestCall.account_id == aid,
                VoiceTestCall.target_phone == "5555999970400",
            )))
            assert len(pending) == 1
            assert pending[0].status == "PENDENTE_PJSIP"
            # No native worker is running in tests, so no phone has been dialed.
            pending[0].status = "EXPIRADO"
            pending[0].finished_at = datetime.now(timezone.utc)
            engine = db.get(VoiceDialerEngine, 1)
            engine.enabled = False
            db.commit()


def test_native_call_rejected_if_engine_off():
    from app.models import VoiceTestCall
    with TestClient(app) as client:
        csrf = _login(client)
        aid, _gid, _cid = _scenario()
        with SessionLocal() as db:
            engine = _ensure_engine(db)
            engine.enabled = False
            engine.test_runtime_enabled = False
            db.commit()
        response = client.post(f"/voice/accounts/{aid}/test-native", data={
            "csrf_token": csrf, "phone": "5555999970500", "authorized": "sim",
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.scalar(select(VoiceTestCall).where(
                VoiceTestCall.account_id == aid, VoiceTestCall.target_phone == "5555999970500",
            )) is None
