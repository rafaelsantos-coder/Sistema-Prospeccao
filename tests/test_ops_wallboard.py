from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIJob,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    User,
)
from app.services.ops_wallboard import build_ops_wallboard
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
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


def test_operations_page_requires_login():
    with TestClient(app) as client:
        response = client.get("/operations", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers.get("location", "")


def test_operations_page_and_api(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        page = client.get("/operations")
        assert page.status_code == 200
        assert "Painel operacional" in page.text
        assert "GESTÃO EM TEMPO REAL" in page.text
        assert "Vendas hoje" in page.text
        assert "Paradas sem venda" in page.text

        api = client.get("/api/operations/wallboard")
        assert api.status_code == 200
        payload = api.json()
        assert "kpis" in payload
        assert "funnel_today" in payload
        assert "queues" in payload
        assert "sellers" in payload
        assert "alerts" in payload
        assert "attention" in payload
        assert payload["refresh_seconds"] == 20


def test_wallboard_counts_open_failed_and_won():
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
        assert admin is not None
        queue = db.scalar(select(ServiceQueue).where(ServiceQueue.code == "WALLBOARD-TEST"))
        if queue is None:
            queue = ServiceQueue(name="Fila wallboard", code="WALLBOARD-TEST", team="DIGITAL", active=True)
            db.add(queue)
            db.flush()

        contact = Contact(name="Cliente Wallboard", phone="5551999000011")
        db.add(contact)
        db.flush()
        conversation = Conversation(
            contact_id=contact.id,
            status="EM_ATENDIMENTO",
            unread_count=2,
            last_message_preview="oi",
            opened_at=utcnow(),
            last_message_at=utcnow(),
        )
        db.add(conversation)
        db.flush()
        db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue.id, origin="INBOUND"))
        inbound = Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="INBOUND",
            message_type="text",
            body="quero internet",
            status="RECEIVED",
        )
        db.add(inbound)
        db.flush()

        agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.active.is_(True)))
        if agent is None:
            agent_user = User(
                name="Sulia Wallboard",
                email="sulia.wallboard@sulnet.local",
                password_hash="x",
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
            )
            db.add(agent_user)
            db.flush()
            agent = AIAgentProfile(
                user_id=agent_user.id,
                name="Sulia",
                slug=f"sulia-wallboard-{agent_user.id}",
                active=True,
            )
            db.add(agent)
            db.flush()

        db.add(
            AIConversationState(
                conversation_id=conversation.id,
                agent_id=agent.id,
                state="AI_ACTIVE",
            )
        )
        db.add(
            AIJob(
                agent_id=agent.id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                message_id=inbound.id,
                status="FALHOU",
                error="timeout provider",
            )
        )

        won_contact = Contact(name="Cliente Ganhou", phone="5551999000012")
        db.add(won_contact)
        db.flush()
        won_conv = Conversation(
            contact_id=won_contact.id,
            status="RESOLVIDO",
            opened_at=utcnow(),
            closed_at=utcnow(),
        )
        db.add(won_conv)
        db.flush()
        db.add(ConversationQueueLink(conversation_id=won_conv.id, queue_id=queue.id, origin="INBOUND"))
        db.add(
            AIClosingState(
                conversation_id=won_conv.id,
                agent_id=agent.id,
                step="COMPLETED",
                external_id=f"wallboard-won-{won_conv.id}",
                completed_at=utcnow(),
                collected_json=json.dumps({"_won_after_signature": True}),
            )
        )
        db.commit()

        snapshot = build_ops_wallboard(db, admin)
        assert snapshot["kpis"]["open"] >= 1
        assert snapshot["kpis"]["failed_ai"] >= 1
        assert snapshot["kpis"]["won_today"] >= 1
        assert snapshot["kpis"]["started_today"] >= 1
        assert any(row["id"] == queue.id for row in snapshot["queues"])
        assert any(alert["code"] == "failed_ai" for alert in snapshot["alerts"])
        assert any(item["conversation_id"] == conversation.id for item in snapshot["attention"])
    finally:
        db.close()


def test_operations_nav_for_admin():
    with TestClient(app) as client:
        _login(client)
        home = client.get("/dashboard")
        assert home.status_code == 200
        assert 'href="/operations"' in home.text
