"""Diagnostico VOZ: nao atendimento != falha tecnica; sem originar chamadas."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import VoiceCallEvent, VoiceCampaign, VoiceRecipient, VoiceSipAccount
from app.services.voice_reporting import campaign_operational_report


def _token(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
    csrf = _token(client.get("/login").text)
    login = client.post("/login", data={
        "csrf_token": csrf,
        "email": "admin@sulnet.local",
        "password": "Sulnet@2026!",
    }, follow_redirects=False)
    assert login.status_code == 303


def _campaign_for_diagnosis() -> int:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        account = VoiceSipAccount(
            name="Diagnostico de falhas " + uuid4().hex[:8],
            domain="sip.example.com", username="1804",
            transport="UDP", active=True, password_encrypted="ci-placeholder",
        )
        db.add(account)
        db.flush()
        campaign = VoiceCampaign(
            name="Diagnostico SIP " + uuid4().hex[:8],
            mode="AUDIO", sip_account_id=account.id, group_id=None,
            audio_uri="s3://ci/camp.wav", status="PAUSADA",
            started_at=now, legal_basis_verified=True,
            window_start="09:00", window_end="18:00",
        )
        db.add(campaign)
        db.flush()
        cases = [
            ("PENDENTE", False, False),
            ("SEM_ATENDIMENTO", False, True),
            ("SEM_ATENDIMENTO", False, True),
            ("SEM_ATENDIMENTO", False, True),
            ("SEM_RESPOSTA", False, True),
            ("OCUPADO", False, True),
            ("FALHOU", False, True),
            ("FALHA_TRANSFERENCIA", True, True),
            ("FINALIZADA", True, True),
            ("SEM_INTERESSE", True, True),
            ("TIMEOUT", False, True),
            ("INTERROMPIDA", False, True),
            ("AUDIO_INTERROMPIDO", True, True),
        ]
        for i, (status, answered, attempted) in enumerate(cases):
            db.add(VoiceRecipient(
                campaign_id=campaign.id, phone=f"555599900{i:04d}",
                name=f"Contato {i}", status=status,
                attempts=int(attempted),
                last_attempt_at=now if attempted else None,
                answered_at=now if answered else None,
            ))
        db.commit()
        return campaign.id


def test_no_answer_does_not_inflate_technical_failures():
    cid = _campaign_for_diagnosis()
    with SessionLocal() as db:
        camp = db.get(VoiceCampaign, cid)
        report = campaign_operational_report(db, camp, [])
        stats = report["stats"]
        assert stats["total"] == 13
        assert stats["pending"] == 1
        assert stats["attempted"] == 12
        assert stats["answered"] == 4
        assert stats["no_answer"] == 4
        assert stats["busy"] == 1
        assert stats["failed"] == 2
        assert stats["timeout"] == 1
        assert stats["without_dtmf"] == 1
        assert stats["interrupted"] == 2
        assert sum(item["count"] for item in report["result_breakdown"]) == 12
        by_status = {row["status"]: row for row in report["result_breakdown"]}
        assert by_status["SEM_ATENDIMENTO"]["count"] == 3
        assert by_status["SEM_ATENDIMENTO"]["label"] == "Sem atendimento"
        assert by_status["FALHA_TRANSFERENCIA"]["label"] == "Erro na transferência"

    with TestClient(app) as client:
        _login(client)
        data = client.get(f"/voice/campaigns/status?campaign_id={cid}")
        assert data.status_code == 200
        report = data.json()["campaigns"][str(cid)]
        assert report["stats"]["failed"] == 2
        assert report["stats"]["no_answer"] == 4
        details = client.get(f"/voice/campaigns/{cid}/details")
        assert details.status_code == 200
        assert "Não atendidas" in details.text
        assert "Falhas técnicas" in details.text
        assert "Diagnóstico por resultado registrado" in details.text
        assert "Sem atendimento" in details.text
        assert "SIP 183" in details.text


def test_call_event_keeps_last_sip_code_on_final_message():
    from app.workers.voice_engine_worker import _set_recipient
    cid = _campaign_for_diagnosis()
    with SessionLocal() as db:
        row = db.scalar(select(VoiceRecipient).where(
            VoiceRecipient.campaign_id == cid,
            VoiceRecipient.status == "PENDENTE",
        ))
        rid = row.id
    _set_recipient(
        rid, "SEM_ATENDIMENTO", 487,
        "Chamada encerrada pelo destino ou pela central.", final=True,
    )
    with SessionLocal() as db:
        row = db.get(VoiceRecipient, rid)
        events = list(db.scalars(select(VoiceCallEvent).where(
            VoiceCallEvent.recipient_id == rid,
        )))
        assert row.status == "SEM_ATENDIMENTO"
        assert len(events) == 1
        assert "ultimo SIP 487" in events[0].detail
        assert "Chamada encerrada" in events[0].detail
