"""Uma mensagem humana antiga nao trava a SulIA que ja assumiu a conversa."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIConversationState, AIQueueBinding,
    Contact, Conversation, ConversationQueueLink, Message, ServiceQueue, User,
)
from app.services.ai_sdr import enqueue_ai_job, human_stepped_in, human_is_attending
from app.services.queue_routing import ai_owns_conversation
from test_ai_all_queues_coverage import _database, _setup


def _scenario(db, *, active_human: bool):
    agent_id, general_id, _, _ = _setup(db)
    queue = db.get(ServiceQueue, general_id)
    queue.name, queue.code = "Geral Comercial", "GERAL"
    binding = db.scalar(select(AIQueueBinding).where(
        AIQueueBinding.queue_id == general_id
    ))
    binding.agent_id = agent_id
    binding.handling_mode = "AI_FIRST"
    binding.active = True
    binding.manual_authorized = True
    ai_agent = db.get(AIAgentProfile, agent_id)

    human = User(
        name="Atendente", email="human-continuity@sulnet.local",
        password_hash="fake", role="CONSULTOR", team="DIGITAL",
        active=True, must_change_password=False,
    )
    db.add(human)
    db.flush()
    contact = Contact(phone="5555999888400", name="Cliente")
    db.add(contact)
    db.flush()
    conv = Conversation(
        contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
        status="EM_ATENDIMENTO",
        assigned_user_id=human.id if active_human else ai_agent.user_id,
    )
    db.add(conv)
    db.flush()
    db.add(ConversationQueueLink(
        conversation_id=conv.id, queue_id=general_id, origin="INBOUND",
    ))
    db.add(AIConversationState(
        conversation_id=conv.id, agent_id=agent_id, state="AI_ACTIVE",
    ))
    manual = Message(
        conversation_id=conv.id, contact_id=contact.id,
        direction="OUTBOUND", message_type="TEXT",
        sender_user_id=human.id, status="SENT",
        body="Olá! Pode me contar do que precisa?",
    )
    db.add(manual)
    db.flush()
    inbound = Message(
        conversation_id=conv.id, contact_id=contact.id,
        direction="INBOUND", message_type="TEXT", status="RECEIVED",
        body="Quero seguir com a contratação",
    )
    db.add(inbound)
    db.flush()
    return conv, inbound


def test_ai_owned_general_continues_after_old_human_message(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            conv, inbound = _scenario(db, active_human=False)
            assert human_stepped_in(db, conv)
            assert ai_owns_conversation(db, conv.id)
            assert not human_is_attending(db, conv)
            job = enqueue_ai_job(db, conversation=conv, message=inbound)
            assert job is not None and job.status == "PENDENTE"
    finally:
        engine.dispose()


def test_real_human_owner_remains_protected(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            conv, inbound = _scenario(db, active_human=True)
            assert human_stepped_in(db, conv)
            assert ai_owns_conversation(db, conv.id)
            assert human_is_attending(db, conv)
            assert enqueue_ai_job(db, conversation=conv, message=inbound) is None
    finally:
        engine.dispose()
