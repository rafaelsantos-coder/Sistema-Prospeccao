from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    ConversationQueueLink,
    ServiceQueue,
)
from app.services.dispatch_queue import (
    claim_next_dispatch_recipient,
    dispatch_counts,
    parse_dispatch_csv,
    process_dispatch_recipient,
)
from app.services.zapi import ZApiService


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/dispatches").text)


def _campaign_and_queue(db, suffix: str):
    campaign = Campaign(
        name=f"Campanha Disparo CSV {suffix}",
        channel="WHATSAPP",
        status="APROVADA",
        daily_limit=100,
        team="DIGITAL",
        sequence_json=json.dumps(
            [{"message": "Olá {{nome}}, mensagem de teste da Sulnet."}],
            ensure_ascii=False,
        ),
    )
    queue = ServiceQueue(
        name=f"Fila Digital Disparo {suffix}",
        code=f"DIGITAL-DISPATCH-{suffix}",
        team="DIGITAL",
        routing_strategy="ROUND_ROBIN",
        active=True,
    )
    db.add_all([campaign, queue])
    db.flush()
    return campaign, queue


def test_parse_dispatch_csv_accepts_name_and_phone():
    rows, summary = parse_dispatch_csv(
        "nome;telefone\nEmpresa A;5555999991001\nEmpresa B;5555999991002\n".encode()
    )
    assert len(rows) == 2
    assert rows[0]["name"] == "Empresa A"
    assert rows[0]["phone"] == "5555999991001"
    assert summary["valid"] == 2
    assert summary["invalid"] == 0


def test_create_dispatch_from_csv():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "CREATE")
            db.commit()
            campaign_id = campaign.id
            queue_id = queue.id
        finally:
            db.close()

        response = client.post(
            "/dispatches",
            data={
                "csrf_token": token,
                "campaign_id": str(campaign_id),
                "queue_id": str(queue_id),
                "interval_seconds": "25",
                "daily_limit": "80",
            },
            files={
                "csv_file": (
                    "lista.csv",
                    "nome;telefone\nEmpresa A;5555999991101\nEmpresa B;5555999991102\n",
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/dispatches/")

        dispatch_id = int(response.headers["location"].rsplit("/", 1)[-1])
        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch is not None
            assert dispatch.status == "RASCUNHO"
            assert dispatch.queue_id == queue_id
            assert dispatch.interval_seconds == 25
            assert dispatch.daily_limit == 80
            counts = dispatch_counts(db, dispatch_id)
            assert counts["total"] == 2
            assert counts["pending"] == 2
        finally:
            db.close()


def test_dispatch_sends_via_zapi_and_preserves_queue(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "DISPATCH-MSG-1",
            "zaapId": "DISPATCH-ZAAP-1",
        },
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "SEND")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=45,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()

            from app.services.dispatch_queue import ensure_dispatch_lead
            lead1 = ensure_dispatch_lead(
                db,
                name="Empresa Sequencial 1",
                phone="5555999991201",
                queue=queue,
                created_by_id=None,
            )
            lead2 = ensure_dispatch_lead(
                db,
                name="Empresa Sequencial 2",
                phone="5555999991202",
                queue=queue,
                created_by_id=None,
            )
            db.add_all(
                [
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        lead_id=lead1.id,
                        name="Empresa Sequencial 1",
                        phone="5555999991201",
                        message_body="Mensagem 1",
                        status="PENDENTE",
                    ),
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        lead_id=lead2.id,
                        name="Empresa Sequencial 2",
                        phone="5555999991202",
                        message_body="Mensagem 2",
                        status="PENDENTE",
                    ),
                ]
            )
            db.commit()
            dispatch_id = dispatch.id

            recipient = claim_next_dispatch_recipient(db)
            assert recipient is not None
            assert recipient.status == "ENVIANDO"
            result = process_dispatch_recipient(db, recipient)
            db.commit()
            assert result == "sent"
            assert recipient.status == "ENVIADO"
            assert recipient.conversation_id is not None

            link = db.scalar(
                select(ConversationQueueLink).where(
                    ConversationQueueLink.conversation_id == recipient.conversation_id
                )
            )
            assert link is not None
            assert link.queue_id == queue.id
            assert link.origin == "DISPATCH"

            immediate_next = claim_next_dispatch_recipient(db)
            assert immediate_next is None

            counts = dispatch_counts(db, dispatch_id)
            assert counts["sent"] == 1
            assert counts["pending"] == 1
        finally:
            db.close()
