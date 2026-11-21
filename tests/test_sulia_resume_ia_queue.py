"""Regressoes: Fila IA responde a compra confirmada apos mensagem manual anterior.

Os testes usam somente SQLite temporario; nao enviam WhatsApp nem consultam CRM.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIJob, AIQueueBinding, Contact, Conversation,
    ConversationQueueLink, Message, ServiceQueue,
)
from app.services.ai_queue_scope import is_dedicated_ai_queue
from app.services.ai_sdr import enqueue_ai_job
from test_ai_all_queues_coverage import _database, _setup


def test_identificacao_da_fila_ia_sem_autorizar_geral_comercial():
    assert is_dedicated_ai_queue(SimpleNamespace(name="Fila IA", code="F-INT", active=True))
    assert is_dedicated_ai_queue(SimpleNamespace(name="Outra", code="FILA-IA", active=True))
    assert is_dedicated_ai_queue(SimpleNamespace(name="  FILA_IA ", code="OUTRA", active=True))
    assert not is_dedicated_ai_queue(SimpleNamespace(name="Geral Comercial", code="GERAL", active=True))
    assert not is_dedicated_ai_queue(SimpleNamespace(name="Fila IA", code="FILA-IA", active=False))
    assert not is_dedicated_ai_queue(None)


def test_cliente_quer_combo_na_fila_ia_e_manual_anterior_nao_silencia(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.setenv("AI_SDR_ENABLED", "true")
    monkeypatch.setenv("AI_SELLER_ENABLED", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, dedicated_id, other_id, _ = _setup(db)
            queue = db.get(ServiceQueue, dedicated_id)
            queue.name = "Fila IA"
            queue.code = "ATENDIMENTO-IA"  # Codigo diferente do legado FILA-IA.
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == dedicated_id))
            binding.handling_mode = "AI_ONLY"
            binding.active = True
            binding.manual_authorized = True
            agent = db.get(AIAgentProfile, agent_id)
            agent.active = True
            contact = Contact(name="Cliente exemplo", phone="5555999888123")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
                assigned_user_id=agent.user_id, status="EM_ATENDIMENTO",
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=dedicated_id))
            inbound = Message(
                conversation_id=conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT", status="RECEIVED",
                body="Quero o combo",
            )
            db.add(inbound)
            db.flush()
            monkeypatch.setattr("app.services.ai_sdr.human_stepped_in", lambda *_args, **_kw: True)
            job = enqueue_ai_job(db, conversation=conversation, message=inbound)
            assert job is not None
            assert job.status == "PENDENTE"
            assert job.queue_id == dedicated_id
            assert db.scalar(select(AIJob).where(AIJob.message_id == inbound.id)).id == job.id

            # A mesma regra nao amplia a IA para outra fila.
            other = Contact(name="Fila humana", phone="5555999888124")
            db.add(other)
            db.flush()
            human_chat = Conversation(contact_id=other.id, source="WHATSAPP", team="DIGITAL", status="EM_FILA")
            db.add(human_chat)
            db.flush()
            db.add(ConversationQueueLink(conversation_id=human_chat.id, queue_id=other_id))
            msg = Message(conversation_id=human_chat.id, contact_id=other.id,
                          direction="INBOUND", message_type="TEXT", status="RECEIVED",
                          body="Quero contratar")
            db.add(msg)
            db.flush()
            assert enqueue_ai_job(db, conversation=human_chat, message=msg) is None
    finally:
        engine.dispose()
