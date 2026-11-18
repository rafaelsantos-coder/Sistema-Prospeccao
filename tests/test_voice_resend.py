"""Reenvio total ou nao atendidos: exclusao historica, blacklist e comando ADMIN.

Os testes nunca executam PJSUA2. Verificam somente selecao persistida e
autorizacao para criar uma nova rodada que o worker processara na producao.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog, GlobalContactBlacklist, VoiceCallEvent, VoiceCampaign,
    VoiceDialerEngine, VoiceGroup, VoiceRecipient, VoiceSipAccount,
)
from app.services.voice_resend import select_resend_recipients


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice/campaigns").text)


def _setup():
    key = uuid4().hex[:10]
    tail = uuid4().int % 1_000_000
    moment = datetime.now(timezone.utc)
    phones = {name: f"55559{tail:06d}{i:02d}" for i, name in enumerate([
        "sem_atendimento", "falha_transferencia", "tocou_falhou",
        "falhou_sem_tocar", "pendente", "blacklist", "respondeu_antes",
        "timeout_ja_atendeu", "ocupado", "audio_interrompido",
        "timeout_sem_atender",
    ], 1)}
    states = {
        "sem_atendimento": "SEM_ATENDIMENTO",
        "falha_transferencia": "FALHA_TRANSFERENCIA",
        "tocou_falhou": "FALHOU",
        "falhou_sem_tocar": "FALHOU",
        "pendente": "PENDENTE",
        "blacklist": "SEM_ATENDIMENTO",
        "respondeu_antes": "SEM_ATENDIMENTO",
        "timeout_ja_atendeu": "TIMEOUT",
        "ocupado": "OCUPADO",
        "audio_interrompido": "AUDIO_INTERROMPIDO",
        "timeout_sem_atender": "TIMEOUT",
    }
    with SessionLocal() as db:
        account = VoiceSipAccount(
            name="Sip resend " + key, username="600", domain="sip.example.com",
            password_encrypted="fake", transport="UDP", active=True,
            sip_check_at=moment, sip_check_code=200,
        )
        group = VoiceGroup(name="Grupo resend " + key, target_extension="300", active=True)
        db.add_all([account, group])
        db.flush()
        source = VoiceCampaign(
            name="Campanha resend " + key, status="CONCLUIDA", mode="DIRECT",
            sip_account_id=account.id, group_id=group.id,
            legal_basis_verified=True, daily_limit=10, interval_seconds=60,
            window_start="09:00", window_end="18:00",
            dial_format="NATIONAL", started_at=moment, stopped_at=moment,
        )
        # Simula uma rodada anterior em que o mesmo telefone atendeu.
        earlier = VoiceCampaign(
            name="Rodada anterior " + key, status="CONCLUIDA", mode="DIRECT",
            sip_account_id=account.id, group_id=group.id,
        )
        db.add_all([source, earlier])
        db.flush()
        source_id, earlier_id = source.id, earlier.id
        recipient_ids = {}
        for name, status in states.items():
            row = VoiceRecipient(
                campaign_id=source_id, name=name, phone=phones[name],
                status=status, attempts=0 if name == "pendente" else 1,
                answered_at=moment if name == "falha_transferencia" else None,
                last_attempt_at=moment if name != "pendente" else None,
            )
            db.add(row)
            db.flush()
            recipient_ids[name] = row.id
        older = VoiceRecipient(
            campaign_id=earlier_id, name="respondeu_antes",
            phone=phones["respondeu_antes"],
            attempts=1, status="TRANSFERIDA", answered_at=moment,
        )
        db.add(older)
        db.flush()
        db.add_all([
            VoiceCallEvent(recipient_id=recipient_ids["tocou_falhou"],
                           event_id=uuid4().hex, event="TOCANDO", detail="SIP 180"),
            VoiceCallEvent(recipient_id=recipient_ids["timeout_ja_atendeu"],
                           event_id=uuid4().hex, event="ATENDIDA", detail="SIP 200"),
        ])
        db.add(GlobalContactBlacklist(
            phone=phones["blacklist"], active=True, name="Optout resend " + key,
            reason="Nao contatar", source="MANUAL",
        ))
        engine = db.get(VoiceDialerEngine, 1)
        existed = engine is not None
        previous = None
        if engine is None:
            engine = VoiceDialerEngine(id=1)
            db.add(engine)
            db.flush()
        else:
            previous = (engine.enabled, engine.campaign_runtime_enabled, engine.heartbeat_at)
        engine.enabled = True
        engine.campaign_runtime_enabled = True
        engine.heartbeat_at = moment
        db.commit()
    return {
        "source_id": source_id, "earlier_id": earlier_id,
        "account_id": account.id, "group_id": group.id,
        "phones": phones, "states": states, "source_name": source.name,
        "engine_existed": existed, "engine_previous": previous,
    }


def _cleanup(config):
    source_id = config["source_id"]
    earlier_id = config["earlier_id"]
    with SessionLocal() as db:
        created = list(db.scalars(select(AuditLog).where(
            AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
            AuditLog.entity_type == "voice_campaign",
            AuditLog.entity_id == str(source_id),
        )))
        clones = [json.loads(row.payload_json)["new_campaign_id"] for row in created]
        ids = [source_id, earlier_id, *clones]
        recipient_ids = select(VoiceRecipient.id).where(VoiceRecipient.campaign_id.in_(ids))
        db.execute(delete(VoiceCallEvent).where(VoiceCallEvent.recipient_id.in_(recipient_ids)))
        db.execute(delete(VoiceRecipient).where(VoiceRecipient.campaign_id.in_(ids)))
        db.execute(delete(AuditLog).where(
            AuditLog.entity_type == "voice_campaign",
            AuditLog.entity_id.in_([str(id_) for id_ in ids]),
        ))
        db.execute(delete(VoiceCampaign).where(VoiceCampaign.id.in_(ids)))
        db.execute(delete(GlobalContactBlacklist).where(
            GlobalContactBlacklist.phone == config["phones"]["blacklist"],
        ))
        db.execute(delete(VoiceGroup).where(VoiceGroup.id == config["group_id"]))
        db.execute(delete(VoiceSipAccount).where(VoiceSipAccount.id == config["account_id"]))
        engine = db.get(VoiceDialerEngine, 1)
        if engine:
            if config["engine_existed"]:
                engine.enabled, engine.campaign_runtime_enabled, engine.heartbeat_at = config["engine_previous"]
            else:
                db.delete(engine)
        db.commit()


def test_resend_selection_excludes_any_answered_calls_and_blacklist():
    with TestClient(app) as client:
        config = _setup()
        try:
            with SessionLocal() as db:
                source = db.get(VoiceCampaign, config["source_id"])
                only = select_resend_recipients(db, source, "UNANSWERED")
                full = select_resend_recipients(db, source, "ALL")
                only_names = {x.name for x in only.eligible}
                full_names = {x.name for x in full.eligible}
                assert only.total == 11
                assert only_names == {
                    "sem_atendimento", "tocou_falhou", "ocupado", "timeout_sem_atender",
                }
                assert only.answered_excluded == 4
                assert only.never_attempted == 1
                assert only.blocked == 1
                assert only.other_excluded == 1
                assert full.eligible_count == 10
                assert "falha_transferencia" in full_names
                assert "blacklist" not in full_names
                assert "pendente" in full_names
            assert client.get(
                f"/voice/campaigns/{config['source_id']}/resend?mode=UNANSWERED",
                follow_redirects=False,
            ).status_code == 303
            token = _login(client)
            page = client.get(f"/voice/campaigns/{config['source_id']}/resend?mode=UNANSWERED",
                              follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"] == f"/voice/campaigns?open={config['source_id']}"
            details = client.get(f"/voice/campaigns/{config['source_id']}/details")
            assert "Reenviar campanha inteira" in details.text
            assert "Reenviar somente não atendidos" in details.text
            assert f'/voice/campaigns/{config["source_id"]}/resend' in details.text
            assert 'name="legal_confirmation"' in details.text
            assert 'name="confirm_name"' not in details.text
        finally:
            _cleanup(config)


def test_resent_campaign_creates_independent_run_and_protects_against_duplicate():
    with TestClient(app) as client:
        config = _setup()
        try:
            token = _login(client)
            endpoint = f"/voice/campaigns/{config['source_id']}/resend"
            payload = {
                "csrf_token": token, "mode": "UNANSWERED",
                "authorization": "sim", "legal_confirmation": "confirmado",
            }
            wrong = client.post(endpoint, data={**payload, "legal_confirmation": ""},
                                follow_redirects=False)
            assert wrong.status_code == 303
            with SessionLocal() as db:
                assert not db.scalar(select(AuditLog.id).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(config["source_id"]),
                ))

            sent = client.post(endpoint, data=payload, follow_redirects=False)
            assert sent.status_code == 303
            with SessionLocal() as db:
                source = db.get(VoiceCampaign, config["source_id"])
                assert source.status == "CONCLUIDA"
                assert source.stopped_at is not None
                assert db.scalar(select(func.count(VoiceRecipient.id)).where(
                    VoiceRecipient.campaign_id == source.id,
                )) == 11
                row = db.scalar(select(AuditLog).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(source.id),
                ))
                assert row is not None
                audit = json.loads(row.payload_json)
                child_id = audit["new_campaign_id"]
                assert sent.headers["location"] == f"/voice/campaigns?open={child_id}"
                assert audit["mode"] == "UNANSWERED"
                assert audit["selected"] == 4
                child = db.get(VoiceCampaign, child_id)
                assert child is not None
                assert child.status == "EM_EXECUCAO"
                assert child.sip_account_id == source.sip_account_id
                assert child.group_id == source.group_id
                assert child.dial_format == source.dial_format
                assert child.daily_limit == source.daily_limit
                assert child.legal_basis_verified is True
                cloned = list(db.scalars(select(VoiceRecipient).where(
                    VoiceRecipient.campaign_id == child_id,
                )))
                assert {x.name for x in cloned} == {
                    "sem_atendimento", "tocou_falhou", "ocupado", "timeout_sem_atender",
                }
                assert all(x.status == "PENDENTE" and x.attempts == 0 for x in cloned)
                assert db.scalar(select(func.count(VoiceCallEvent.id)).join(
                    VoiceRecipient, VoiceCallEvent.recipient_id == VoiceRecipient.id,
                ).where(VoiceRecipient.campaign_id == child_id)) == 0

            again = client.post(endpoint, data=payload, follow_redirects=False)
            assert again.status_code == 303
            with SessionLocal() as db:
                assert db.scalar(select(func.count(AuditLog.id)).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(config["source_id"]),
                )) == 1
        finally:
            _cleanup(config)


def test_full_resend_can_recontact_answered_but_not_blacklisted():
    with TestClient(app) as client:
        config = _setup()
        try:
            token = _login(client)
            payload = {
                "csrf_token": token, "mode": "ALL",
                "authorization": "sim", "legal_confirmation": "confirmado",
            }
            response = client.post(
                f"/voice/campaigns/{config['source_id']}/resend",
                data=payload, follow_redirects=False,
            )
            assert response.status_code == 303
            with SessionLocal() as db:
                row = db.scalar(select(AuditLog).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(config["source_id"]),
                ))
                audit = json.loads(row.payload_json)
                assert audit["mode"] == "ALL"
                assert audit["selected"] == 10
                child = db.get(VoiceCampaign, audit["new_campaign_id"])
                rows = list(db.scalars(select(VoiceRecipient).where(
                    VoiceRecipient.campaign_id == child.id,
                )))
                assert len(rows) == 10
                assert "falha_transferencia" in {x.name for x in rows}
                assert "pendente" in {x.name for x in rows}
                assert "blacklist" not in {x.name for x in rows}
        finally:
            _cleanup(config)


def test_unanswered_resend_blocked_if_engine_off_or_source_active():
    with TestClient(app) as client:
        config = _setup()
        try:
            token = _login(client)
            payload = {
                "csrf_token": token, "mode": "UNANSWERED",
                "confirm_name": config["source_name"],
                "authorization": "sim", "legal_confirmation": "confirmado",
            }
            with SessionLocal() as db:
                db.get(VoiceDialerEngine, 1).enabled = False
                db.commit()
            blocked = client.post(
                f"/voice/campaigns/{config['source_id']}/resend",
                data=payload, follow_redirects=False,
            )
            assert blocked.status_code == 303
            with SessionLocal() as db:
                assert db.scalar(select(AuditLog.id).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_BLOCKED",
                    AuditLog.entity_id == str(config["source_id"]),
                )) is not None
                assert db.scalar(select(AuditLog.id).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(config["source_id"]),
                )) is None
                db.get(VoiceDialerEngine, 1).enabled = True
                db.get(VoiceCampaign, config["source_id"]).status = "EM_EXECUCAO"
                db.commit()
            assert client.get(
                f"/voice/campaigns/{config['source_id']}/resend?mode=ALL",
                follow_redirects=False,
            ).status_code == 303
            blocked_active = client.post(
                f"/voice/campaigns/{config['source_id']}/resend",
                data=payload, follow_redirects=False,
            )
            assert blocked_active.status_code == 303
            with SessionLocal() as db:
                assert db.scalar(select(AuditLog.id).where(
                    AuditLog.action == "VOICE_CAMPAIGN_RESEND_CREATED",
                    AuditLog.entity_id == str(config["source_id"]),
                )) is None
        finally:
            _cleanup(config)
