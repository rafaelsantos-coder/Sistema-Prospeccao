"""Pacing: resultado ambiguo nao pode parecer falha confirmada nem repetir envio."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, PacedOutboundMessage
from app.services.paced_outbound import process_one_pending, queue_manual_message
from app.services.zapi import ZApiError, ZApiService
from app.services.zapi_instances import bind_conversation_instance, create_zapi_instance
from app.timeutils import utcnow


def _prepare_due_message() -> tuple[int, int]:
    with SessionLocal() as db:
        suffix = uuid4().hex[:10]
        instance = create_zapi_instance(
            db, name="Teste pacing " + suffix, phone_label="Teste",
            base_url="https://api.z-api.io",
            instance_id="paced-unknown-" + suffix,
            instance_token="dummy-token", client_token="dummy-client",
            created_by_id=None,
        )
        # Telefone ficticio unico entre execucoes da suite.
        phone = f"55559{int(suffix[:8], 16) % 100_000_000:08d}"
        contact = Contact(name="Contato simulado", phone=phone)
        db.add(contact)
        db.flush()
        conversation = Conversation(
            contact_id=contact.id, status="NOVO",
            source="WHATSAPP", team="DIGITAL",
        )
        db.add(conversation)
        db.flush()
        bind_conversation_instance(db, conversation.id, instance.id)
        message, slot = queue_manual_message(
            db, conversation, message_type="TEXT",
            body="Mensagem apenas de teste interno", sender_user_id=1,
        )
        slot.scheduled_at = utcnow() - timedelta(seconds=1)
        db.commit()
        return message.id, slot.id


def test_manual_timeout_is_unknown_and_never_silently_retries(monkeypatch):
    called = []
    def no_confirmation(self, *, phone, message):
        called.append(phone)
        raise ZApiError("Timeout simulado apos aceite", status_code=503)

    monkeypatch.setattr(ZApiService, "send_text", no_confirmation)
    with TestClient(app):
        msg_id, slot_id = _prepare_due_message()
        assert process_one_pending() == "unknown"
        with SessionLocal() as db:
            assert db.get(PacedOutboundMessage, slot_id).status == "UNKNOWN"
            assert db.get(Message, msg_id).status == "UNKNOWN"
        # UNKNOWN nao corresponde ao predicado de claim PENDING.
        assert len(called) == 1


def test_manual_deterministic_rejection_remains_failed(monkeypatch):
    def rejected(self, *, phone, message):
        raise ZApiError("400 malformado antes de aceitar", status_code=400)

    monkeypatch.setattr(ZApiService, "send_text", rejected)
    with TestClient(app):
        msg_id, slot_id = _prepare_due_message()
        assert process_one_pending() == "failed"
        with SessionLocal() as db:
            assert db.get(PacedOutboundMessage, slot_id).status == "FAILED"
            assert db.get(Message, msg_id).status == "FAILED"


def test_orphaned_processing_becomes_unknown_and_unblocks_next_contact(monkeypatch):
    """Reinicio apos SIGKILL nao repete o antigo nem paralisa o mesmo numero."""
    sent = []

    def accepted(self, *, phone, message):
        sent.append(message)
        return {"messageId": "ACK-TEST-SAFE"}

    monkeypatch.setattr(ZApiService, "send_text", accepted)
    with TestClient(app):
        msg_id, slot_id = _prepare_due_message()
        with SessionLocal() as db:
            first = db.get(PacedOutboundMessage, slot_id)
            first.status = "PROCESSING"
            first.claimed_at = utcnow() - timedelta(minutes=30)
            first_message = db.get(Message, msg_id)
            conversation = db.get(Conversation, first_message.conversation_id)
            next_msg, next_slot = queue_manual_message(
                db, conversation, message_type="TEXT",
                body="Segundo contato ficticio", sender_user_id=1,
            )
            next_slot.scheduled_at = utcnow() - timedelta(seconds=1)
            db.commit()
            next_ids = next_msg.id, next_slot.id

        assert process_one_pending() == "sent"
        with SessionLocal() as db:
            assert db.get(PacedOutboundMessage, slot_id).status == "UNKNOWN"
            assert db.get(Message, msg_id).status == "UNKNOWN"
            assert db.get(PacedOutboundMessage, next_ids[1]).status == "SENT"
            assert db.get(Message, next_ids[0]).status == "SENT"
        assert sent == ["Segundo contato ficticio"]
