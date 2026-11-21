"""Recupera clientes sem resposta apos reparar a autorizacao da Fila IA."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AIAgentProfile, AIJob, AIQueueBinding, Contact, Conversation,
    ConversationQueueLink, Message, ServiceQueue,
)
from app.timeutils import utcnow
from app.workers import ai_sdr_worker
from test_ai_all_queues_coverage import _database, _setup


def test_review_recovers_ignored_job_from_disabled_fila_ia_once(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            agent_id, general_id, ai_queue_id, _ = _setup(db)
            q = db.get(ServiceQueue, ai_queue_id)
            q.name, q.code = "Fila IA", "IA-PRODUCAO"
            agent = db.get(AIAgentProfile, agent_id)
            agent.active = True
            agent.execution_mode = "AUTO"
            binding = AIQueueBinding(
                queue_id=ai_queue_id, agent_id=agent_id,
                handling_mode="AI_ONLY", active=True,
                manual_authorized=True, fallback_to_human=False,
            )
            db.add(binding)
            contact = Contact(name="Cliente teste", phone="5555987658881")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, source="WHATSAPP",
                team="DIGITAL", status="EM_FILA",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=ai_queue_id,
            ))
            inbound = Message(
                conversation_id=conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT", status="RECEIVED",
                body="Quero o combo", created_at=utcnow(),
            )
            db.add(inbound)
            db.flush()
            job = AIJob(
                agent_id=agent_id, queue_id=ai_queue_id,
                conversation_id=conversation.id, message_id=inbound.id,
                status="IGNORADO", error="IA pausada na fila da conversa.",
            )
            db.add(job)
            db.commit()
            job_id = job.id
            inbound_id = inbound.id

        monkeypatch.setattr(ai_sdr_worker, "SessionLocal", factory)
        first = ai_sdr_worker._review_yesterday_and_today_fila_ia()
        assert first.get("requeued_binding_error") == 1
        with factory() as db:
            job = db.get(AIJob, job_id)
            assert job.status == "PENDENTE"
            assert job.error == "REQUEUED_DEDICATED_AI_QUEUE_20261121"
            assert job.queue_id == ai_queue_id

        second = ai_sdr_worker._review_yesterday_and_today_fila_ia()
        assert second.get("requeued_binding_error", 0) == 0
        with factory() as db:
            assert len(list(db.scalars(
                select(AIJob).where(AIJob.message_id == inbound_id)
            ))) == 1
    finally:
        engine.dispose()
