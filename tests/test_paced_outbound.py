"""Pacing das mensagens iniciais de varios consultores no mesmo numero."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, PacedOutboundMessage
from app.services.paced_outbound import (
    needs_pacing, process_one_pending, queue_manual_message,
)
from app.services.zapi import ZApiService
from app.services.zapi_instances import bind_conversation_instance, create_zapi_instance
from app.timeutils import utcnow, utc_naive


def test_same_instance_serializes_first_contacts_and_sends_later(monkeypatch):
    sent = []
    monkeypatch.setattr(
        ZApiService, "send_text",
        lambda self, *, phone, message: (
            sent.append((self.instance_id, phone, message))
            or {"messageId": "TEST-PACED-" + str(len(sent))}
        ),
    )
    with TestClient(app):
        with SessionLocal() as db:
            instance = create_zapi_instance(
                db, name="Pacing test", phone_label="Test",
                base_url="https://api.z-api.io",
                instance_id="paced-test-instance",
                instance_token="safe-test-token", client_token="safe-client-token",
                created_by_id=None,
            )
            conversations = []
            for index in range(2):
                contact = Contact(
                    name="Contato " + str(index),
                    phone="5591999000" + str(100 + index),
                )
                db.add(contact)
                db.flush()
                conv = Conversation(
                    contact_id=contact.id, status="NOVO",
                    source="WHATSAPP", team="DIGITAL",
                )
                db.add(conv)
                db.flush()
                bind_conversation_instance(db, conv.id, instance.id)
                conversations.append(conv.id)
            db.commit()

            created = []
            for index, conv_id in enumerate(conversations):
                conv = db.get(Conversation, conv_id)
                assert needs_pacing(db, conv)
                msg, slot = queue_manual_message(
                    db, conv, message_type="TEXT",
                    body="Primeira mensagem " + str(index),
                    sender_user_id=1,
                )
                db.commit()
                created.append((msg.id, slot.id, slot.scheduled_at))

            assert not sent
            assert (utc_naive(created[1][2]) - utc_naive(created[0][2])).total_seconds() >= 30
            assert all(db.get(Message, item[0]).status == "PENDING" for item in created)
            for _, slot_id, _ in created:
                db.get(PacedOutboundMessage, slot_id).scheduled_at = utcnow() - timedelta(seconds=1)
            db.commit()

        assert process_one_pending() == "sent"
        assert process_one_pending() == "sent"
        assert len(sent) == 2
        assert [x[2] for x in sent] == ["Primeira mensagem 0", "Primeira mensagem 1"]
        with SessionLocal() as db:
            for message_id, slot_id, _ in created:
                assert db.get(Message, message_id).status == "SENT"
                assert db.get(PacedOutboundMessage, slot_id).status == "SENT"


def test_answered_conversation_bypasses_pacing():
    from app.services.conversation_engine import register_message
    with TestClient(app):
        with SessionLocal() as db:
            contact = Contact(name="Respondente", phone="5591999000999")
            db.add(contact)
            db.flush()
            conv = Conversation(
                contact_id=contact.id, status="EM_ATENDIMENTO",
                source="WHATSAPP", team="DIGITAL",
            )
            db.add(conv)
            db.flush()
            assert needs_pacing(db, conv)
            register_message(
                db, conv, direction="INBOUND",
                message_type="TEXT", body="Tenho interesse!",
            )
            db.flush()
            assert not needs_pacing(db, conv)
            db.rollback()
