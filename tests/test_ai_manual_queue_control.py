"""Controle do SulIA por fila: nao autorizar atendimento sem ativacao gerencial."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.main import app
from app.models import AIAgentProfile, AIQueueBinding, Contact, Conversation, ConversationQueueLink, ServiceQueue, ServiceQueueZApiInstance
from app.services.ai_queue_coverage import ensure_unanswered_coverage_all_queues
from app.services.ai_queue_scope import (
    ai_allowed_for_conversation,
    ai_allowed_for_queue,
    ai_queue_enabled,
    exclusive_ai_queue_id,
    manual_queue_control_enabled,
)
from app.services.ai_sdr import binding_for_conversation, queue_ai_binding
from app.services.queue_routing import usable_ai_binding
from app.services.zapi_instances import create_zapi_instance
from test_ai_all_queues_coverage import _database, _setup


def test_existing_active_bindings_fail_closed_without_manager_approval(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "1")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, first_id, other_id, _ = _setup(db)
            db.add(AIQueueBinding(queue_id=other_id, agent_id=agent_id, handling_mode="AI_FIRST", active=True))
            db.commit()

            assert manual_queue_control_enabled()
            assert exclusive_ai_queue_id() is None
            assert ai_allowed_for_queue(first_id)
            assert ai_allowed_for_queue(other_id)
            assert not ai_queue_enabled(db, other_id)
            assert queue_ai_binding(db, other_id) is None
            assert usable_ai_binding(db, other_id) is None

            contact = Contact(phone="5555999999912", name="Cliente humano")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, source="WHATSAPP", status="EM_FILA")
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=other_id))
            db.commit()
            assert not ai_allowed_for_conversation(db, conversation.id)
            assert binding_for_conversation(db, conversation.id) is None

            result = ensure_unanswered_coverage_all_queues(db)
            assert result["enabled"] == []
            assert result["manual_control"] is True

            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == other_id))
            binding.manual_authorized = True
            db.commit()
            assert ai_queue_enabled(db, other_id)
            assert ai_allowed_for_conversation(db, conversation.id)
            assert usable_ai_binding(db, other_id) is not None
            assert binding_for_conversation(db, conversation.id) is not None

            binding.active = False
            db.commit()
            assert not ai_queue_enabled(db, other_id)
            assert not ai_allowed_for_conversation(db, conversation.id)
    finally:
        engine.dispose()


def _login_admin(client):
    login = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
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


def _csrf(html):
    token = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert token
    return token.group(1)


def test_manager_can_configure_then_activate_and_pause_one_queue(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "1")
    suffix = uuid4().hex[:8].upper()
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.role_type == "SDR"))
            assert agent is not None
            agent.active = True
            queue = ServiceQueue(name=f"Fila individual {suffix}", code=f"M-{suffix}", team="DIGITAL", active=True)
            db.add(queue)
            db.commit()
            queue_id, agent_id = queue.id, agent.id
        finally:
            db.close()

        _login_admin(client)
        page = client.get("/ai")
        assert page.status_code == 200
        assert f"Fila individual {suffix}" in page.text
        assert 'Ativação individual' in page.text or 'ativação individual' in page.text

        response = client.post(
            "/ai/queues/bind",
            data={
                "csrf_token": _csrf(page.text),
                "queue_id": str(queue_id),
                "agent_id": str(agent_id),
                "handling_mode": "AI_FIRST",
                "fallback_to_human": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id))
            assert binding is not None
            assert not binding.active
            assert not binding.manual_authorized

        page = client.get("/ai")
        enabled = client.post(
            f"/ai/queues/{queue_id}/toggle",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        with SessionLocal() as db:
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id))
            assert binding.active and binding.manual_authorized
            assert ai_queue_enabled(db, queue_id)

        page = client.get("/ai")
        stopped = client.post(
            f"/ai/queues/{queue_id}/toggle",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert stopped.status_code == 303
        with SessionLocal() as db:
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id))
            assert not binding.active
            assert not binding.manual_authorized
            assert not ai_queue_enabled(db, queue_id)


def test_unlinked_zapi_connection_appears_in_selector_without_enabling_ai(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "1")
    suffix = uuid4().hex[:12]
    with TestClient(app) as client:
        with SessionLocal() as db:
            agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.role_type == "SDR"))
            assert agent is not None
            agent.active = True
            instance = create_zapi_instance(
                db,
                name=f"Fila ZAPI Sem Vínculo {suffix}",
                phone_label="Telefone teste",
                base_url="https://api.z-api.io",
                instance_id=f"zapi-select-unlinked-{suffix}",
                instance_token="instance-test-token-not-real",
                client_token="client-test-token-not-real",
                created_by_id=None,
            )
            db.commit()
            instance_id, agent_id = instance.id, agent.id

        _login_admin(client)
        page = client.get("/ai")
        assert page.status_code == 200
        assert f'value="zapi:{instance_id}"' in page.text
        assert f"Fila ZAPI Sem Vínculo {suffix}" in page.text

        # Navegar pelo menu não pode criar uma fila operacional,
        # nem inscrever a IA numa instância.
        with SessionLocal() as db:
            assert not db.scalar(
                select(ServiceQueueZApiInstance.id)
                .where(ServiceQueueZApiInstance.zapi_instance_id == instance_id)
            )

        response = client.post(
            "/ai/queues/bind",
            data={
                "csrf_token": _csrf(page.text),
                "queue_id": f"zapi:{instance_id}",
                "agent_id": str(agent_id),
                "handling_mode": "AI_FIRST",
                "fallback_to_human": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        with SessionLocal() as db:
            link = db.scalar(
                select(ServiceQueueZApiInstance)
                .where(ServiceQueueZApiInstance.zapi_instance_id == instance_id)
            )
            assert link is not None
            queue = db.get(ServiceQueue, link.queue_id)
            binding = db.scalar(
                select(AIQueueBinding)
                .where(AIQueueBinding.queue_id == queue.id)
            )
            assert queue.name == f"Fila ZAPI Sem Vínculo {suffix}"
            assert queue.active
            assert not queue.is_default_inbound
            assert binding is not None
            assert binding.agent_id == agent_id
            assert binding.active is False
            assert binding.manual_authorized is False
            assert ai_queue_enabled(db, queue.id) is False

        # Após salvar, a mesma instância é exibida como fila operacional,
        # sem opção virtual duplicada.
        page_after = client.get("/ai")
        assert f'value="zapi:{instance_id}"' not in page_after.text
        assert f"Fila ZAPI Sem Vínculo {suffix}" in page_after.text
