from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Contact,
    Conversation,
    ConversationQueueLink,
    ConversationZApiInstance,
    ServiceQueue,
    ServiceQueueZApiInstance,
    User,
)
from app.services.secret_store import decrypt_secret
from app.services.zapi import ZApiNotConfigured, ZApiService
from app.services.queue_routing import bind_conversation_queue
from app.services.zapi_instances import (
    bind_conversation_instance,
    bind_queue_instance,
    conversation_instance,
    create_zapi_instance,
    service_for_conversation,
    service_for_queue,
)


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
    return _csrf(client.get("/settings").text)


def _instance(db, suffix: str):
    admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
    item = create_zapi_instance(
        db,
        name=f"WhatsApp {suffix}",
        phone_label=f"55559999{suffix[-2:] if len(suffix) >= 2 else '00'}",
        base_url="https://api.z-api.io",
        instance_id=f"instance-{suffix}",
        instance_token=f"token-{suffix}-secret",
        client_token=f"client-{suffix}-secret",
        created_by_id=admin.id if admin else None,
    )
    db.commit()
    db.refresh(item)
    return item


def test_zapi_instance_secrets_are_encrypted_at_rest():
    with TestClient(app):
        db = SessionLocal()
        try:
            item = _instance(db, "ENCRYPT")
            assert "token-ENCRYPT-secret" not in item.instance_token_encrypted
            assert "client-ENCRYPT-secret" not in item.client_token_encrypted
            assert decrypt_secret(item.instance_token_encrypted) == "token-ENCRYPT-secret"
            assert decrypt_secret(item.client_token_encrypted) == "client-ENCRYPT-secret"
        finally:
            db.close()


def test_queue_creation_binds_selected_whatsapp_instance():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            item = _instance(db, "B2B-CREATE")
            instance_id = item.id
        finally:
            db.close()

        response = client.post(
            "/settings/queues",
            data={
                "csrf_token": token,
                "name": "Fila B2B Teste",
                "code": "B2B-INSTANCE-TEST",
                "team": "B2B",
                "branch_id": "",
                "routing_strategy": "ROUND_ROBIN",
                "zapi_instance_id": str(instance_id),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            queue = db.scalar(
                select(ServiceQueue).where(ServiceQueue.code == "B2B-INSTANCE-TEST")
            )
            assert queue is not None
            link = db.scalar(
                select(ServiceQueueZApiInstance).where(
                    ServiceQueueZApiInstance.queue_id == queue.id
                )
            )
            assert link is not None
            assert link.zapi_instance_id == instance_id
        finally:
            db.close()


def test_dispatch_service_uses_instance_bound_to_queue(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    def fake_send(self, *, phone, message):
        sent.append((self.instance_id, phone, message))
        return {"messageId": "MULTI-INSTANCE-1", "zaapId": "ZAAP-MULTI-1"}

    monkeypatch.setattr(ZApiService, "send_text", fake_send)

    from app.models import Campaign, CampaignDispatch, CampaignDispatchRecipient
    from app.services.dispatch_queue import process_dispatch_recipient

    with TestClient(app):
        db = SessionLocal()
        try:
            item = _instance(db, "DISPATCH")
            queue = ServiceQueue(
                name="Fila Dispatch Multi",
                code="DISPATCH-MULTI-INSTANCE",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            campaign = Campaign(
                name="Campanha Multi",
                channel="WHATSAPP",
                status="APROVADA",
                daily_limit=100,
                team="B2B",
                sequence_json='[{"message":"Olá {{nome}}"}]',
            )
            db.add_all([queue, campaign])
            db.flush()
            bind_queue_instance(db, queue.id, item.id)

            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=10,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Cliente B2B",
                phone="5555999991888",
                message_body="Mensagem B2B",
                status="ENVIANDO",
            )
            db.add(recipient)
            db.commit()

            outcome = process_dispatch_recipient(db, recipient)
            db.commit()

            assert outcome == "sent"
            assert sent == [("instance-DISPATCH", "5555999991888", "Mensagem B2B")]
            assert recipient.conversation_id is not None
            conversation_instance = db.scalar(
                select(ConversationZApiInstance).where(
                    ConversationZApiInstance.conversation_id == recipient.conversation_id
                )
            )
            assert conversation_instance is not None
            assert conversation_instance.zapi_instance_id == item.id
        finally:
            db.close()


def test_instance_webhook_routes_new_inbound_to_instance_queue():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            item = _instance(db, "INBOUND")
            queue = ServiceQueue(
                name="Fila B2B Inbound",
                code="B2B-INBOUND-INSTANCE",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                is_default_inbound=True,
                active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_instance(db, queue.id, item.id)
            db.commit()
            webhook_key = item.webhook_key
            instance_id = item.instance_id
            queue_id = queue.id
            zapi_db_id = item.id
        finally:
            db.close()

        response = client.post(
            f"/webhooks/zapi/instance/{webhook_key}",
            json={
                "instanceId": instance_id,
                "phone": "5555999991777",
                "senderName": "Cliente B2B",
                "messageId": "INBOUND-MULTI-INSTANCE-1",
                "text": {"message": "Quero falar com o B2B"},
                "fromMe": False,
                "status": "RECEIVED",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["created"] is True
        conversation_id = payload["conversation_id"]

        db = SessionLocal()
        try:
            queue_link = db.scalar(
                select(ConversationQueueLink).where(
                    ConversationQueueLink.conversation_id == conversation_id
                )
            )
            assert queue_link is not None
            assert queue_link.queue_id == queue_id

            instance_link = db.scalar(
                select(ConversationZApiInstance).where(
                    ConversationZApiInstance.conversation_id == conversation_id
                )
            )
            assert instance_link is not None
            assert instance_link.zapi_instance_id == zapi_db_id
        finally:
            db.close()


def test_conversation_output_always_follows_current_queue_instance():
    with TestClient(app):
        db = SessionLocal()
        try:
            old_instance = _instance(db, "STALE-CONVERSATION")
            queue_instance_item = _instance(db, "QUEUE-AUTHORITATIVE")

            queue = ServiceQueue(
                name="Fila com número próprio",
                code="QUEUE-AUTHORITATIVE-ZAPI",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            contact = Contact(
                phone="5555999991666",
                name="Cliente Multi Número",
                business_name="Cliente Multi Número",
            )
            db.add_all([queue, contact])
            db.flush()

            conversation = Conversation(
                contact_id=contact.id,
                team="DIGITAL",
                status="EM_FILA",
            )
            db.add(conversation)
            db.flush()

            bind_queue_instance(db, queue.id, queue_instance_item.id)
            bind_conversation_queue(
                db,
                conversation.id,
                queue.id,
                origin="TEST",
            )
            # Simula uma conversa antiga que ainda estava presa ao número principal/antigo.
            bind_conversation_instance(db, conversation.id, old_instance.id)
            db.commit()

            service = service_for_conversation(db, conversation.id)

            assert service.instance_id == queue_instance_item.instance_id
            repaired = conversation_instance(db, conversation.id)
            assert repaired is not None
            assert repaired.id == queue_instance_item.id
        finally:
            db.close()


def test_queue_without_instance_never_falls_back_to_principal():
    with TestClient(app):
        db = SessionLocal()
        try:
            queue = ServiceQueue(
                name="Fila Sem Número",
                code="QUEUE-WITHOUT-ZAPI",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.commit()

            with pytest.raises(ZApiNotConfigured):
                service_for_queue(db, queue.id)
        finally:
            db.close()
