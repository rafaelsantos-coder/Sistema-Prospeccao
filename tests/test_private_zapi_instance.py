"""A private Z-API number is visible only to its designated consultant.

Regression for Maria Gabriela's dedicated number, including reports, media and
cross-instance lifetime history for the same phone number.
"""
from sqlalchemy import select
from fastapi import HTTPException
import pytest

from app.models import Contact, Conversation, ConversationZApiInstance, Message, User, ZApiInstance
from app.services.conversation_engine import apply_conversation_scope
from app.services.instance_privacy import (
    require_conversation_instance_access, visible_message_clause,
)
from app.routers.conversations import _contact_history_messages, _message_visible_through_contact
from app.database import SessionLocal
from app.main import app
from fastapi.testclient import TestClient


def test_private_instance_denies_everyone_except_owner():
    with TestClient(app):
        db = SessionLocal()
        try:
            users = []
            for name, role in (("Maria Gabriela", "CONSULTOR"), ("Outro vendedor", "CONSULTOR"),
                               ("Gerente", "GESTOR"), ("Admin", "ADMIN")):
                item = User(name=name, email=f"exclusive-{len(users)}@sulnet.local",
                            password_hash="unused", role=role, team="VISIB", active=True)
                db.add(item)
                db.flush()
                users.append(item)
            maria, other, manager, admin = users
            instance = ZApiInstance(name="Maria Gabriela - Sulnet", phone_label="+55 (55) 99900-0524",
                                    base_url="https://api.z-api.io", instance_id="exclusive-test-A50443D0",
                                    instance_token_encrypted="unused", client_token_encrypted="",
                                    webhook_key="exclusive-test-webhook-key", exclusive_access=True,
                                    exclusive_user_id=maria.id)
            db.add(instance)
            db.flush()
            contact = Contact(phone="5555999000524", name="Contato de teste")
            db.add(contact)
            db.flush()
            private = Conversation(contact_id=contact.id, assigned_user_id=maria.id,
                                   team="VISIB", source="WHATSAPP", status="EM_ATENDIMENTO")
            public = Conversation(contact_id=contact.id, assigned_user_id=other.id,
                                  team="VISIB", source="WHATSAPP", status="EM_ATENDIMENTO")
            db.add_all([private, public])
            db.flush()
            db.add(ConversationZApiInstance(conversation_id=private.id, zapi_instance_id=instance.id))
            db.add_all([
                Message(conversation_id=private.id, contact_id=contact.id,
                        direction="INBOUND", body="mensagem privada"),
                Message(conversation_id=public.id, contact_id=contact.id,
                        direction="INBOUND", body="mensagem aberta"),
            ])
            db.flush()
            for actor in (other, manager, admin):
                scoped = apply_conversation_scope(
                    select(Conversation.id).where(Conversation.id.in_((private.id, public.id))), actor
                )
                assert private.id not in set(db.scalars(scoped))
                assert [m.body for m in _contact_history_messages(db, public, actor)] == ["mensagem aberta"]
                with pytest.raises(HTTPException) as error:
                    require_conversation_instance_access(db, actor, private.id)
                assert error.value.status_code == 404
                priv_msg = db.scalar(select(Message).where(Message.conversation_id == private.id))
                with pytest.raises(HTTPException):
                    _message_visible_through_contact(db, actor, priv_msg)
            assert private.id in set(db.scalars(apply_conversation_scope(
                select(Conversation.id).where(Conversation.id == private.id), maria
            )))
            assert {m.body for m in _contact_history_messages(db, private, maria)} == {
                "mensagem privada", "mensagem aberta"
            }
            instance.exclusive_user_id = None
            db.flush()
            assert private.id not in set(db.scalars(apply_conversation_scope(
                select(Conversation.id).where(Conversation.id == private.id), maria
            )))
        finally:
            db.rollback()
            db.close()
