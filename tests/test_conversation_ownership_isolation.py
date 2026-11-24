"""Conversa iniciada/atendida por um vendedor não pode migrar para outro.

Regressão do caso Norival: Claudia atende o cliente e a resposta aparece
para Maria Gabriela como se ela fosse a responsável.
"""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Contact,
    Conversation,
    ConversationAssignment,
    ConversationQueueLink,
    ConversationZApiInstance,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.conversation_engine import apply_conversation_scope
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _phone(suffix: str, mid: str) -> str:
    """Telefone BR canônico de 13 dígitos (55 + DDD 55 + 9 + assinador)."""
    digits = "".join(ch for ch in suffix if ch.isdigit()) or "1234"
    # Ex.: 5555992712345 — normalize_phone mantém como está.
    return f"555599{mid.zfill(2)[:2]}{digits.zfill(4)[:4]}"


def test_exclusive_webhook_does_not_steal_active_seller(monkeypatch):
    """Webhook da instância exclusiva da Maria não remove a Claudia do atendimento."""
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            maria = User(
                name="Maria Gabriela Isolamento",
                email=f"maria.iso.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaMaria@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            claudia = User(
                name="Claudia Jose Isolamento",
                email=f"claudia.iso.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaClaudia@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add_all([maria, claudia])
            db.flush()
            instance = create_zapi_instance(
                db,
                name=f"Maria exclusiva {suffix}",
                phone_label="+55 (55) 99900-0524",
                base_url="https://api.z-api.io",
                instance_id=f"exclusive-iso-A50443D0-{suffix}",
                instance_token=f"token-{suffix}",
                client_token=f"client-{suffix}",
                created_by_id=None,
            )
            instance.exclusive_access = True
            instance.exclusive_user_id = maria.id
            queue = ServiceQueue(
                name=f"Geral Comercial Iso {suffix}",
                code=f"GERAL-ISO-{suffix}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=claudia.id, active=True))
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=maria.id, active=True))
            bind_queue_instance(db, queue.id, instance.id)
            contact = Contact(phone=_phone(suffix, "17"), name="Norival Isolamento")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=claudia.id,
                team="DIGITAL",
                source="MANUAL",
                status="EM_ATENDIMENTO",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=queue.id, origin="MANUAL",
            ))
            db.add(ConversationZApiInstance(
                conversation_id=conversation.id, zapi_instance_id=instance.id,
            ))
            db.add(Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="OUTBOUND",
                message_type="TEXT",
                body="sou Claudia, o senhor esteve aqui em loja",
                sender_user_id=claudia.id,
                status="SENT",
            ))
            db.add(ConversationAssignment(
                conversation_id=conversation.id,
                user_id=claudia.id,
                assigned_by_id=claudia.id,
                event="CLAIMED",
            ))
            ids = {
                "maria": maria.id,
                "claudia": claudia.id,
                "conversation": conversation.id,
                "phone": contact.phone,
                "webhook": instance.webhook_key,
                "instance": instance.instance_id,
            }
            db.commit()
        finally:
            db.close()

        inbound = {
            "type": "ReceivedCallback",
            "instanceId": ids["instance"],
            "phone": ids["phone"],
            "fromMe": False,
            "messageId": f"INBOUND-ISO-{suffix}",
            "text": {"message": "Claudia me liga"},
            "status": "RECEIVED",
        }
        response = client.post(f"/webhooks/zapi/instance/{ids['webhook']}", json=inbound)
        assert response.status_code == 200
        assert response.json().get("created") is True

        with SessionLocal() as db:
            conversation = db.get(Conversation, ids["conversation"])
            assert conversation.assigned_user_id == ids["claudia"]
            scoped_maria = apply_conversation_scope(
                select(Conversation.id).where(Conversation.id == ids["conversation"]),
                db.get(User, ids["maria"]),
                inbox=True,
            )
            scoped_claudia = apply_conversation_scope(
                select(Conversation.id).where(Conversation.id == ids["conversation"]),
                db.get(User, ids["claudia"]),
                inbox=True,
            )
            assert ids["conversation"] not in set(db.scalars(scoped_maria))
            assert ids["conversation"] in set(db.scalars(scoped_claudia))


def test_exclusive_webhook_keeps_recent_writer_when_unowned():
    """Se a Claudia já escreveu e a conversa ficou sem dono, a resposta não vai para a Maria."""
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            maria = User(
                name="Maria Gabriela Unowned",
                email=f"maria.unowned.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaMaria@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            claudia = User(
                name="Claudia Jose Unowned",
                email=f"claudia.unowned.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaClaudia@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add_all([maria, claudia])
            db.flush()
            instance = create_zapi_instance(
                db,
                name=f"Maria exclusiva unowned {suffix}",
                phone_label="+55 (55) 99900-0524",
                base_url="https://api.z-api.io",
                instance_id=f"exclusive-unowned-A50443D0-{suffix}",
                instance_token=f"token-u-{suffix}",
                client_token=f"client-u-{suffix}",
                created_by_id=None,
            )
            instance.exclusive_access = True
            instance.exclusive_user_id = maria.id
            contact = Contact(phone=_phone(suffix, "27"), name="Cliente Sem Dono")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=None,
                team="DIGITAL",
                source="WHATSAPP",
                status="EM_FILA",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationZApiInstance(
                conversation_id=conversation.id, zapi_instance_id=instance.id,
            ))
            db.add(Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="OUTBOUND",
                message_type="TEXT",
                body="Seu filho esteve aqui e conversamos sobre a internet",
                sender_user_id=claudia.id,
                status="SENT",
            ))
            ids = {
                "maria": maria.id,
                "claudia": claudia.id,
                "conversation": conversation.id,
                "phone": contact.phone,
                "webhook": instance.webhook_key,
                "instance": instance.instance_id,
            }
            db.commit()
        finally:
            db.close()

        inbound = {
            "type": "ReceivedCallback",
            "instanceId": ids["instance"],
            "phone": ids["phone"],
            "fromMe": False,
            "messageId": f"INBOUND-UNOWNED-{suffix}",
            "text": {"message": "ok"},
            "status": "RECEIVED",
        }
        response = client.post(f"/webhooks/zapi/instance/{ids['webhook']}", json=inbound)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload.get("ok") is True, payload
        assert payload.get("conversation_id") == ids["conversation"], payload

        with SessionLocal() as db:
            from app.models import ZApiInstance
            instance = db.scalar(
                select(ZApiInstance).where(ZApiInstance.webhook_key == ids["webhook"])
            )
            assert instance is not None
            assert instance.exclusive_access is True
            assert instance.exclusive_user_id == ids["maria"]
            conversation = db.get(Conversation, ids["conversation"])
            assert conversation.assigned_user_id == ids["claudia"], (
                conversation.assigned_user_id,
                ids["claudia"],
                ids["maria"],
                payload,
            )
            assert conversation.assigned_user_id != ids["maria"]


def test_reply_claims_unowned_conversation(monkeypatch):
    """Responder na caixa assume a conversa para quem escreveu."""
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    monkeypatch.setattr(
        "app.services.zapi.ZApiService.send_text",
        lambda self, *, phone, message: {"messageId": "CLAIM-REPLY-1", "zaapId": "ZAAP-1"},
    )
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            seller = User(
                name="Claudia Claim Reply",
                email=f"claudia.claim.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaClaudia@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add(seller)
            db.flush()
            queue = ServiceQueue(
                name=f"Fila Claim {suffix}",
                code=f"CLAIM-{suffix}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True))
            instance = create_zapi_instance(
                db,
                name=f"Z-API Claim {suffix}",
                phone_label="Claim",
                base_url="https://api.z-api.io",
                instance_id=f"claim-reply-{suffix}",
                instance_token=f"token-{suffix}",
                client_token=f"client-{suffix}",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            contact = Contact(phone=_phone(suffix, "37"), name="Cliente Claim")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=None,
                team="DIGITAL",
                source="WHATSAPP",
                status="EM_FILA",
                unread_count=1,
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=queue.id, origin="INBOUND",
            ))
            db.add(ConversationZApiInstance(
                conversation_id=conversation.id, zapi_instance_id=instance.id,
            ))
            db.add(Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="TEXT",
                body="Quero falar com a Claudia",
                status="RECEIVED",
            ))
            seller_id, conversation_id, email = seller.id, conversation.id, seller.email
            db.commit()
        finally:
            db.close()

        login = client.get("/login")
        token = _csrf(login.text)
        assert client.post(
            "/login",
            data={"email": email, "password": "SenhaClaudia@2026", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        dash = client.get("/dashboard")
        token = _csrf(dash.text)
        assert client.post("/api/whatsapp-presence/join", data={"csrf_token": token}).status_code == 200

        page = client.get(f"/conversations/{conversation_id}")
        assert page.status_code == 200
        token = _csrf(page.text)
        sent = client.post(
            f"/conversations/{conversation_id}/send",
            data={"csrf_token": token, "message": "sou Claudia, podemos falar"},
            follow_redirects=False,
        )
        assert sent.status_code == 303

        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            assert conversation.assigned_user_id == seller_id
            claim = db.scalar(
                select(ConversationAssignment).where(
                    ConversationAssignment.conversation_id == conversation_id,
                    ConversationAssignment.event.in_(("CLAIMED_OPEN", "CLAIMED_REPLY")),
                    ConversationAssignment.user_id == seller_id,
                )
            )
            assert claim is not None
