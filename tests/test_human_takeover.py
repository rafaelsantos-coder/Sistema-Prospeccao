"""Tomada de controle humano persistente, sem desligar a IA das outras conversas."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIConversationState, AIFollowupJob, AIJob, AIQueueBinding, AIReplyDelivery,
    Contact, Conversation, ConversationAssignment, ConversationQueueLink,
    Message, ServiceQueue, User,
)
from app.routers import conversations
from app.services.ai_queue_scope import (
    AI_RESUMED_EVENT, HUMAN_TAKEOVER_EVENT, ai_allowed_for_conversation,
    human_takeover_locked,
)
from app.services.ai_sdr import human_is_attending, enqueue_ai_job
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database, _setup


def test_human_takeover_cancels_only_selected_chat_and_persists_until_resume(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, queue_id, _, _ = _setup(db)
            queue = db.get(ServiceQueue, queue_id)
            queue.name = "Fila IA"
            queue.code = "FILA-IA"
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id))
            binding.handling_mode = "AI_ONLY"
            binding.active = True
            binding.manual_authorized = True
            human = User(name="Operador", email="takeover-test@sulnet.local",
                         password_hash="fake", role="ADMIN", team="DIGITAL",
                         active=True, must_change_password=False)
            db.add(human)
            db.flush()
            records = []
            for index in (1, 2):
                contact = Contact(phone=f"555599998880{index}")
                db.add(contact)
                db.flush()
                chat = Conversation(contact_id=contact.id, source="WHATSAPP",
                                    team="DIGITAL", status="EM_FILA")
                db.add(chat)
                db.flush()
                db.add(ConversationQueueLink(conversation_id=chat.id, queue_id=queue_id))
                db.add(AIConversationState(conversation_id=chat.id, agent_id=agent_id, state="CLOSING"))
                incoming = Message(conversation_id=chat.id, contact_id=contact.id,
                                   direction="INBOUND", message_type="TEXT",
                                   body="Quero saber os planos", status="RECEIVED")
                outgoing = Message(conversation_id=chat.id, contact_id=contact.id,
                                   direction="OUTBOUND", message_type="TEXT",
                                   body="Resposta pendente", status="PENDING")
                db.add(incoming)
                db.flush()
                db.add(outgoing)
                db.flush()
                db.add(AIReplyDelivery(inbound_message_id=incoming.id,
                                       outbound_message_id=outgoing.id, agent_id=agent_id))
                job = AIJob(conversation_id=chat.id, agent_id=agent_id, queue_id=queue_id,
                            message_id=incoming.id, status="PENDENTE")
                followup = AIFollowupJob(conversation_id=chat.id, agent_id=agent_id,
                                        queue_id=queue_id, due_at=utcnow(), status="PENDENTE")
                db.add_all([job, followup])
                records.append((chat, incoming, outgoing, job, followup))
            db.commit()
            first, incoming, reply, job, followup = records[0]
            second, _, second_reply, second_job, second_followup = records[1]
            monkeypatch.setattr(conversations, "_user_or_redirect", lambda *_: (human, None))
            monkeypatch.setattr(conversations, "validate_csrf", lambda *_: None)
            monkeypatch.setattr(conversations, "require_roles", lambda *_: None)
            monkeypatch.setattr(conversations, "has_permission", lambda *_: True)
            monkeypatch.setattr(conversations, "_get_conversation", lambda db, user, cid: db.get(Conversation, cid))
            monkeypatch.setattr(conversations, "flash", lambda *args, **kwargs: None)

            response = conversations.takeover_conversation_human(first.id, None, "test", db)
            assert response.status_code == 303
            assert first.assigned_user_id == human.id
            assert first.status == "EM_ATENDIMENTO"
            assert human_takeover_locked(db, first.id)
            assert human_is_attending(db, first)
            assert not ai_allowed_for_conversation(db, first.id)
            assert enqueue_ai_job(db, conversation=first, message=incoming) is None
            assert job.status == "CANCELADO"
            assert followup.status == "CANCELADO"
            assert reply.status == "CANCELED"
            assert ai_allowed_for_conversation(db, second.id)
            assert second_job.status == "PENDENTE"
            assert second_followup.status == "PENDENTE"
            assert second_reply.status == "PENDING"
            assert db.scalar(select(ConversationQueueLink.queue_id).where(
                ConversationQueueLink.conversation_id == first.id)) == queue_id
            assert db.scalar(select(ConversationAssignment.event).where(
                ConversationAssignment.conversation_id == first.id)) == HUMAN_TAKEOVER_EVENT

            first_id = first.id
            # Conferir a persistência em nova sessão, sem qualquer fala humana.
        with Session(engine) as db:
            assert human_takeover_locked(db, first_id)
            assert not ai_allowed_for_conversation(db, first_id)
            db.add(ConversationAssignment(conversation_id=first_id, event=AI_RESUMED_EVENT))
            db.commit()
            assert not human_takeover_locked(db, first_id)
            assert ai_allowed_for_conversation(db, first_id)
    finally:
        engine.dispose()
