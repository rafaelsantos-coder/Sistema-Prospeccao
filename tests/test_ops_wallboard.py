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
from app.services.ops_wallboard import build_ops_wallboard, safe_build_ops_wallboard
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


def test_safe_build_never_raises(monkeypatch):
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
        assert admin is not None

        def boom(*_args, **_kwargs):
            raise RuntimeError("falha simulada do wallboard")

        monkeypatch.setattr("app.services.ops_wallboard.build_ops_wallboard", boom)
        snapshot = safe_build_ops_wallboard(db, admin)
        assert snapshot["kpis"]["open"] == 0
        assert any(alert["code"] == "wallboard_error" for alert in snapshot["alerts"])
    finally:
        db.close()


def test_operations_page_survives_total_failure(monkeypatch):
    with TestClient(app) as client:
        _login(client)

        def boom(*_args, **_kwargs):
            raise RuntimeError("falha total")

        monkeypatch.setattr("app.services.ops_wallboard.build_ops_wallboard", boom)
        page = client.get("/operations")
        assert page.status_code == 200
        assert "Painel operacional" in page.text
        assert "Falha ao carregar métricas" in page.text
        assert page.headers.get("X-Ops-Revision")

        rev = client.get("/api/operations/revision")
        assert rev.status_code == 200
        assert "revision" in rev.json()


def test_wallboard_survives_invalid_collected_json():
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
        assert admin is not None
        queue = db.scalar(select(ServiceQueue).where(ServiceQueue.code == "WALLBOARD-JSON"))
        if queue is None:
            queue = ServiceQueue(name="Fila json", code="WALLBOARD-JSON", team="DIGITAL", active=True)
            db.add(queue)
            db.flush()
        agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.active.is_(True)))
        assert agent is not None
        contact = Contact(name="Cliente JSON", phone="5551999000088")
        db.add(contact)
        db.flush()
        conversation = Conversation(
            contact_id=contact.id,
            status="EM_ATENDIMENTO",
            opened_at=utcnow(),
            last_message_at=utcnow() - __import__("datetime").timedelta(minutes=40),
        )
        db.add(conversation)
        db.flush()
        db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue.id, origin="INBOUND"))
        db.add(
            Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="text",
                body="oi",
                status="RECEIVED",
                created_at=utcnow() - __import__("datetime").timedelta(minutes=40),
            )
        )
        db.add(
            AIClosingState(
                conversation_id=conversation.id,
                agent_id=agent.id,
                step="WAITING_CRM_READY",
                external_id=f"wallboard-json-{conversation.id}",
                last_error="falha crm",
                collected_json="[1,2,3]",
                updated_at=utcnow(),
            )
        )
        db.commit()
        snapshot = build_ops_wallboard(db, admin)
        assert snapshot["kpis"]["stalled_without_sale"] >= 1
    finally:
        db.close()


def test_wallboard_survives_stuck_sales_without_set_crash():
    """Regressão do 500 em /operations: stuck_sales são dicts e não podem ir para set."""
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
        assert admin is not None
        queue = db.scalar(select(ServiceQueue).where(ServiceQueue.code == "WALLBOARD-STUCK"))
        if queue is None:
            queue = ServiceQueue(name="Fila stuck", code="WALLBOARD-STUCK", team="DIGITAL", active=True)
            db.add(queue)
            db.flush()

        agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.active.is_(True)))
        if agent is None:
            agent_user = User(
                name="Sulia Stuck",
                email="sulia.stuck@sulnet.local",
                password_hash="x",
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
            )
            db.add(agent_user)
            db.flush()
            agent = AIAgentProfile(
                user_id=agent_user.id,
                name="Sulia Stuck",
                slug=f"sulia-stuck-{agent_user.id}",
                active=True,
            )
            db.add(agent)
            db.flush()

        contact = Contact(name="Cliente Stuck", phone="5551999000099")
        db.add(contact)
        db.flush()
        conversation = Conversation(
            contact_id=contact.id,
            status="EM_ATENDIMENTO",
            unread_count=1,
            opened_at=utcnow(),
            last_message_at=utcnow() - __import__("datetime").timedelta(minutes=30),
        )
        db.add(conversation)
        db.flush()
        db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue.id, origin="INBOUND"))
        db.add(
            Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="text",
                body="quero fechar",
                status="RECEIVED",
                created_at=utcnow() - __import__("datetime").timedelta(minutes=30),
            )
        )
        db.add(
            AIConversationState(
                conversation_id=conversation.id,
                agent_id=agent.id,
                state="CLOSING",
            )
        )
        db.add(
            AIClosingState(
                conversation_id=conversation.id,
                agent_id=agent.id,
                step="WAITING_CRM_READY",
                external_id=f"wallboard-stuck-{conversation.id}",
                last_error="plano não encontrado no CRM",
                plan_label="800 Mega",
                collected_json=json.dumps({"name": "Cliente Stuck"}),
                updated_at=utcnow(),
            )
        )
        db.commit()

        snapshot = build_ops_wallboard(db, admin)
        assert snapshot["kpis"]["stalled_without_sale"] >= 1
        assert any(alert["code"] == "stuck_sale" for alert in snapshot["alerts"])
        assert any(item["conversation_id"] == conversation.id for item in snapshot["attention"])
    finally:
        db.close()
