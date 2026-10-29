from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Contact,
    Conversation,
    ConversationQueueLink,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.conversation_engine import ensure_contact, ensure_open_conversation, register_message, store_received_payload
from app.services.queue_routing import assign_queue_user, bind_conversation_queue, queue_for_conversation
from app.services.whatsapp_presence import join_whatsapp
from app.services.zapi_instances import create_zapi_instance
from app.timeutils import utcnow


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
    page = client.get("/settings")
    return _csrf(page.text)


def _user(db, email: str, name: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    user = User(
        name=name,
        email=email,
        password_hash=hash_password("SenhaFila@2026"),
        role="CONSULTOR",
        team="DIGITAL",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def test_create_queue_from_settings():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            u1 = _user(db, "fila-criar-1@sulnet.local", "Fila Criar 1")
            u2 = _user(db, "fila-criar-2@sulnet.local", "Fila Criar 2")
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            instance = create_zapi_instance(
                db,
                name="WhatsApp Teste Filas",
                phone_label="Teste",
                base_url="https://api.z-api.io",
                instance_id="instance-service-queue-test",
                instance_token="token-service-queue-test",
                client_token="client-service-queue-test",
                created_by_id=admin.id if admin else None,
            )
            db.commit()
            ids = [u1.id, u2.id]
            instance_id = instance.id
        finally:
            db.close()

        response = client.post(
            "/settings/queues",
            data={
                "csrf_token": token,
                "name": "Digital Teste Config",
                "code": "DIGITAL-TESTE-CONFIG",
                "team": "DIGITAL",
                "routing_strategy": "ROUND_ROBIN",
                "zapi_instance_id": str(instance_id),
                "member_user_ids": [str(ids[0]), str(ids[1])],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/settings/queues/")

        page = client.get(response.headers["location"])
        assert page.status_code == 200
        assert "Digital Teste Config" in page.text
        assert "Fila Criar 1" in page.text
        assert "Fila Criar 2" in page.text


def test_round_robin_uses_only_online_queue_members():
    with TestClient(app):
        db = SessionLocal()
        try:
            u1 = _user(db, "rr-1@sulnet.local", "Round Robin 1")
            u2 = _user(db, "rr-2@sulnet.local", "Round Robin 2")
            offline = _user(db, "rr-offline@sulnet.local", "Round Robin Offline")
            queue = ServiceQueue(
                name="Fila Round Robin",
                code="QUEUE-RR-TEST",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add(queue)
            db.flush()
            for user in (u1, u2, offline):
                db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id))
            join_whatsapp(db, u1)
            join_whatsapp(db, u2)
            db.commit()

            first = assign_queue_user(db, queue)
            second = assign_queue_user(db, queue)
            assert first is not None
            assert second is not None
            assert first.id != second.id
            assert offline.id not in {first.id, second.id}
        finally:
            db.close()


def test_least_open_prefers_operator_with_less_pending_work():
    with TestClient(app):
        db = SessionLocal()
        try:
            u1 = _user(db, "least-1@sulnet.local", "Least Open 1")
            u2 = _user(db, "least-2@sulnet.local", "Least Open 2")
            queue = ServiceQueue(
                name="Fila Menor Carga",
                code="QUEUE-LEAST-TEST",
                team="DIGITAL",
                routing_strategy="LEAST_OPEN",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add_all([
                ServiceQueueMember(queue_id=queue.id, user_id=u1.id),
                ServiceQueueMember(queue_id=queue.id, user_id=u2.id),
            ])
            join_whatsapp(db, u1)
            join_whatsapp(db, u2)

            contact = Contact(
                phone="5555988800011",
                name="Carga Um",
                business_name="Carga Um",
            )
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=u1.id,
                team="DIGITAL",
                status="EM_ATENDIMENTO",
                opened_at=utcnow(),
            )
            db.add(conversation)
            db.flush()
            bind_conversation_queue(
                db,
                conversation.id,
                queue.id,
                origin="TEST",
            )
            db.commit()

            selected = assign_queue_user(db, queue)
            assert selected is not None
            assert selected.id == u2.id
        finally:
            db.close()


def test_outbound_queue_waits_for_reply_before_assignment():
    with TestClient(app):
        db = SessionLocal()
        try:
            user = _user(db, "reply-queue@sulnet.local", "Operador Resposta")
            queue = ServiceQueue(
                name="Fila Resposta",
                code="QUEUE-REPLY-TEST",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id))
            join_whatsapp(db, user)

            contact = ensure_contact(db, "5555988800022", name="Cliente Resposta")
            conversation = ensure_open_conversation(
                db,
                contact,
                source="CAMPAIGN:TEST",
                queue_id=queue.id,
                queue_origin="CAMPAIGN",
                assign_if_unowned=False,
            )
            register_message(
                db,
                conversation,
                direction="OUTBOUND",
                message_type="TEXT",
                body="Mensagem de prospecção",
                status="SENT",
            )
            db.commit()

            assert conversation.assigned_user_id is None
            assert conversation.status == "AGUARDANDO_CLIENTE"
            linked = queue_for_conversation(db, conversation.id)
            assert linked is not None
            assert linked.id == queue.id

            inbound_conversation, inbound_message, created = store_received_payload(
                db,
                {
                    "phone": "5555988800022",
                    "messageId": "QUEUE-REPLY-INBOUND-1",
                    "senderName": "Cliente Resposta",
                    "text": {"message": "Tenho interesse"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True
            assert inbound_message is not None
            assert inbound_conversation is not None
            assert inbound_conversation.assigned_user_id == user.id
            assert inbound_conversation.status == "EM_ATENDIMENTO"
        finally:
            db.close()


def test_default_inbound_queue_routes_spontaneous_contact():
    with TestClient(app):
        db = SessionLocal()
        try:
            user = _user(db, "default-inbound@sulnet.local", "Operador Entrada")
            queue = ServiceQueue(
                name="Fila Entrada Padrão",
                code="QUEUE-DEFAULT-INBOUND-TEST",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                is_default_inbound=True,
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id))
            join_whatsapp(db, user)
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": "5555988800033",
                    "messageId": "QUEUE-SPONTANEOUS-1",
                    "senderName": "Novo Cliente",
                    "text": {"message": "Olá, gostaria de informações"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True
            assert conversation is not None
            assert message is not None
            assert conversation.assigned_user_id == user.id
            linked = db.scalar(
                select(ConversationQueueLink).where(
                    ConversationQueueLink.conversation_id == conversation.id
                )
            )
            assert linked is not None
            assert linked.queue_id == queue.id
        finally:
            db.close()
