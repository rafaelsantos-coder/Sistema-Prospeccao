"""Conversa particular: só o responsável vê; nova conversa comercial nasce limpa.

Regressão do caso Loja Três de Maio: chat pessoal Maria↔contato não pode
vazar para a fila, e outro vendedor que inicia atendimento depois não herda
o histórico particular.
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
    ConversationQueueLink,
    ConversationZApiInstance,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.conversation_engine import (
    apply_conversation_scope,
    ensure_open_conversation,
)
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _phone(suffix: str) -> str:
    digits = "".join(ch for ch in suffix if ch.isdigit()) or "4242"
    return f"555593{digits.zfill(7)[:7]}"


def test_private_conversation_hidden_from_queue_and_admin():
    suffix = uuid4().hex[:8]
    with TestClient(app):
        db = SessionLocal()
        try:
            maria = User(
                name="Maria Particular",
                email=f"maria.priv.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaMaria@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            claudia = User(
                name="Claudia Fila",
                email=f"claudia.priv.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaClaudia@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            admin = User(
                name="Admin Fila",
                email=f"admin.priv.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaAdmin@2026"),
                role="ADMIN",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add_all([maria, claudia, admin])
            db.flush()
            queue = ServiceQueue(
                name=f"Loja Tres de Maio {suffix}",
                code=f"LOJA-TM-{suffix}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add_all([
                ServiceQueueMember(queue_id=queue.id, user_id=maria.id, active=True),
                ServiceQueueMember(queue_id=queue.id, user_id=claudia.id, active=True),
            ])
            contact = Contact(phone=_phone(suffix), name="Rafael Particular")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=maria.id,
                team="DIGITAL",
                source="WHATSAPP",
                status="ENCERRADO",
                private_to_assignee=True,
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=queue.id, origin="INBOUND",
            ))
            db.add(Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="TEXT",
                body="Segredo só entre Rafael e Maria",
                status="RECEIVED",
            ))
            ids = {
                "maria": maria.id,
                "claudia": claudia.id,
                "admin": admin.id,
                "conversation": conversation.id,
            }
            db.commit()
        finally:
            db.close()

        with SessionLocal() as db:
            for actor_id, should_see in (
                (ids["maria"], True),
                (ids["claudia"], False),
                (ids["admin"], False),
            ):
                actor = db.get(User, actor_id)
                scoped = apply_conversation_scope(
                    select(Conversation.id).where(Conversation.id == ids["conversation"]),
                    actor,
                    inbox=False,
                )
                visible = ids["conversation"] in set(db.scalars(scoped))
                assert visible is should_see, actor.email


def test_new_commercial_conversation_does_not_inherit_private_history(monkeypatch):
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            maria = User(
                name="Maria Hist Priv",
                email=f"maria.hist.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaMaria@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            claudia = User(
                name="Claudia Hist Priv",
                email=f"claudia.hist.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaClaudia@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add_all([maria, claudia])
            db.flush()
            queue = ServiceQueue(
                name=f"Fila Comercial {suffix}",
                code=f"COM-{suffix}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add_all([
                ServiceQueueMember(queue_id=queue.id, user_id=maria.id, active=True),
                ServiceQueueMember(queue_id=queue.id, user_id=claudia.id, active=True),
            ])
            instance = create_zapi_instance(
                db,
                name=f"Z-API Loja {suffix}",
                phone_label="Loja",
                base_url="https://api.z-api.io",
                instance_id=f"loja-priv-{suffix}",
                instance_token=f"token-{suffix}",
                client_token=f"client-{suffix}",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            contact = Contact(phone=_phone(suffix + "9"), name="Rafael Comercial")
            db.add(contact)
            db.flush()
            private = Conversation(
                contact_id=contact.id,
                assigned_user_id=maria.id,
                team="DIGITAL",
                source="WHATSAPP",
                status="ENCERRADO",
                private_to_assignee=True,
            )
            db.add(private)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=private.id, queue_id=queue.id, origin="INBOUND",
            ))
            db.add(ConversationZApiInstance(
                conversation_id=private.id, zapi_instance_id=instance.id,
            ))
            db.add(Message(
                conversation_id=private.id,
                contact_id=contact.id,
                direction="OUTBOUND",
                message_type="TEXT",
                body="Mensagem particular que a fila nao pode ver",
                sender_user_id=maria.id,
                status="SENT",
            ))
            ids = {
                "claudia": claudia.id,
                "email": claudia.email,
                "queue": queue.id,
                "phone": contact.phone,
                "private": private.id,
                "contact": contact.id,
            }
            db.commit()
        finally:
            db.close()

        login = client.get("/login")
        token = _csrf(login.text)
        assert client.post(
            "/login",
            data={
                "email": ids["email"],
                "password": "SenhaClaudia@2026",
                "csrf_token": token,
            },
            follow_redirects=False,
        ).status_code == 303
        dash = client.get("/dashboard")
        token = _csrf(dash.text)
        assert client.post(
            "/api/whatsapp-presence/join", data={"csrf_token": token}
        ).status_code == 200

        # Conversa particular encerrada nao aparece para a Claudia.
        hidden = client.get(
            f"/conversations/{ids['private']}", follow_redirects=False
        )
        assert hidden.status_code == 303
        assert "Segredo" not in (hidden.headers.get("location") or "")
        inbox = client.get("/conversations")
        assert "Mensagem particular que a fila nao pode ver" not in inbox.text

        page = client.get("/conversations")
        token = _csrf(page.text)
        started = client.post(
            "/conversations/start",
            data={
                "csrf_token": token,
                "phone": ids["phone"],
                "queue_id": str(ids["queue"]),
                "template_id": "",
            },
            headers={"Accept": "application/json"},
        )
        assert started.status_code == 200, started.text
        payload = started.json()
        assert payload["ok"] is True
        assert payload["conversation_id"] != ids["private"]

        with SessionLocal() as db:
            commercial = db.get(Conversation, payload["conversation_id"])
            assert commercial is not None
            assert commercial.private_to_assignee is False
            assert commercial.assigned_user_id == ids["claudia"]
            bodies = list(
                db.scalars(
                    select(Message.body).where(
                        Message.conversation_id == commercial.id
                    )
                )
            )
            assert "Mensagem particular que a fila nao pode ver" not in bodies
            # Historico particular permanece isolado na conversa antiga.
            private_bodies = list(
                db.scalars(
                    select(Message.body).where(Message.conversation_id == ids["private"])
                )
            )
            assert private_bodies == ["Mensagem particular que a fila nao pode ver"]


def test_exclusive_instance_marks_conversation_private():
    suffix = uuid4().hex[:8]
    with TestClient(app):
        db = SessionLocal()
        try:
            maria = User(
                name="Maria Exclusiva Priv",
                email=f"maria.excl.{suffix}@sulnet.local",
                password_hash=hash_password("SenhaMaria@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add(maria)
            db.flush()
            instance = create_zapi_instance(
                db,
                name=f"WhatsApp Maria {suffix}",
                phone_label="+55 (55) 99900-0524",
                base_url="https://api.z-api.io",
                instance_id=f"exclusive-priv-{suffix}",
                instance_token=f"token-{suffix}",
                client_token=f"client-{suffix}",
                created_by_id=None,
            )
            instance.exclusive_access = True
            instance.exclusive_user_id = maria.id
            contact = Contact(phone=_phone(suffix + "1"), name="Contato Exclusivo")
            db.add(contact)
            db.flush()
            conversation = ensure_open_conversation(
                db,
                contact,
                source="WHATSAPP",
                zapi_instance_id=instance.id,
                assign_if_unowned=True,
            )
            assert conversation.private_to_assignee is True
            assert conversation.assigned_user_id == maria.id
        finally:
            db.rollback()
            db.close()
