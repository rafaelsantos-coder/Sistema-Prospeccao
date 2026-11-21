"""Regressoes: jobs antigos nao perdem a posse da SulIA na Fila IA.

Banco temporario em memoria; nenhum contato com WhatsApp ou CRM.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIQueueBinding,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
)
from app.services import ai_sdr
from test_ai_all_queues_coverage import _database, _setup


@pytest.mark.parametrize("initial_state", ["NEW", "SHADOW_REVIEW", None])
def test_fila_ia_reconciles_stale_job_and_activates_owner_before_reply(
    monkeypatch, initial_state,
):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, ai_queue_id, obsolete_queue_id, _ = _setup(db)
            queue = db.get(ServiceQueue, ai_queue_id)
            queue.name = "Fila IA"
            queue.code = "FILA-IA"
            agent = db.get(AIAgentProfile, agent_id)
            agent.active = True
            binding = db.scalar(
                select(AIQueueBinding).where(AIQueueBinding.queue_id == ai_queue_id)
            )
            binding.handling_mode = "AI_ONLY"
            binding.active = True
            binding.manual_authorized = True
            contact = Contact(name="Lead de teste", phone="5555999888000")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
                status="EM_FILA",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=ai_queue_id,
            ))
            inbound = Message(
                conversation_id=conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT",
                body="Quero contratar", status="RECEIVED",
            )
            db.add(inbound)
            db.flush()
            if initial_state is not None:
                db.add(AIConversationState(
                    conversation_id=conversation.id,
                    agent_id=agent_id,
                    state=initial_state,
                ))
            job = AIJob(
                agent_id=agent_id, queue_id=obsolete_queue_id,
                conversation_id=conversation.id, message_id=inbound.id,
                status="PENDENTE",
            )
            db.add(job)
            db.flush()

            # O caminho sintetico para antes da chamada ao provedor de IA,
            # preservando as transicoes efetivas do process_ai_job.
            monkeypatch.setattr(ai_sdr, "build_ai_context", lambda *_a, **_kw: {})
            monkeypatch.setattr(
                ai_sdr.dispatch_offer, "source_dispatch", lambda *_a, **_kw: None,
            )
            monkeypatch.setattr(
                ai_sdr, "_contextual_service_reply", lambda *_a, **_kw: None,
            )
            monkeypatch.setattr(
                ai_sdr, "_try_recall_path", lambda *_a, **_kw: None,
            )
            monkeypatch.setattr(
                ai_sdr, "_try_fast_purchase_path", lambda *_a, **_kw: "synthetic",
            )
            assert ai_sdr.process_ai_job(db, job) == "synthetic"
            state = db.scalar(select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id,
            ))
            assert state.state == "AI_ACTIVE"
            assert state.agent_id == agent_id
            assert job.queue_id == ai_queue_id
    finally:
        engine.dispose()


def test_fila_humana_does_not_activate_or_reconcile_job(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, _, human_queue_id, _ = _setup(db)
            queue = db.get(ServiceQueue, human_queue_id)
            queue.name = "Geral Comercial"
            queue.code = "GERAL"
            contact = Contact(name="Lead humano", phone="5555999888111")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
                status="EM_FILA",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=human_queue_id,
            ))
            inbound = Message(
                conversation_id=conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT",
                body="Preciso de vendedor", status="RECEIVED",
            )
            db.add(inbound)
            db.flush()
            job = AIJob(
                agent_id=agent_id, queue_id=human_queue_id,
                conversation_id=conversation.id, message_id=inbound.id,
                status="PENDENTE",
            )
            db.add(job)
            db.flush()
            assert ai_sdr.process_ai_job(db, job) == "skipped"
            assert job.status == "CANCELADO"
            assert db.scalar(select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id,
            )) is None
    finally:
        engine.dispose()
