"""Incidente 21/11: Geral Comercial so passa a IA por transferencia manual."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIConversationState, AIFollowupJob, AIJob, AIQueueBinding,
    AIAgentProfile, Contact, Conversation, ConversationQueueLink,
    Message, ServiceQueue,
)
from app.services.ai_queue_scope import (
    ai_allowed_for_conversation, ai_queue_enabled,
    enforce_general_commercial_human_only, is_general_commercial_queue,
)
from app.services.ai_sdr import (
    AIConfigurationError, _send_autonomous_reply,
    binding_for_conversation, enqueue_ai_job, process_ai_job,
)
from app.services.ai_reply_delivery import queue_ai_reply
from app.services.ai_followups import process_followup_job
from app.services.ai_seller import _assert_closer_owns_conversation
from app.services.crm_sulnet import CRMIntegrationError
from app.services.queue_routing import (
    MANUAL_AI_ORIGIN, bind_conversation_queue, usable_ai_binding,
)
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database, _setup


def _case(db: Session):
    agent_id, first_id, _, _ = _setup(db)
    general = ServiceQueue(
        name="Geral Comercial", code="GERAL-COMERCIAL", team="DIGITAL", active=True,
    )
    db.add(general)
    db.flush()
    db.add(AIQueueBinding(
        queue_id=general.id, agent_id=agent_id,
        handling_mode="AI_WHEN_HUMAN_IDLE", active=True,
        manual_authorized=True,
    ))
    user_id = db.get(AIAgentProfile, agent_id).user_id
    contact = Contact(phone="5555987659901", name="Contato da Geral")
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id, status="EM_ATENDIMENTO",
        assigned_user_id=user_id,
    )
    db.add(conversation)
    db.flush()
    db.add(ConversationQueueLink(
        conversation_id=conversation.id, queue_id=general.id, origin="INBOUND",
    ))
    inbound = Message(
        conversation_id=conversation.id, contact_id=contact.id,
        direction="INBOUND", message_type="TEXT", body="Bom dia", status="RECEIVED",
    )
    db.add(inbound)
    db.flush()
    db.add(AIConversationState(
        conversation_id=conversation.id, agent_id=agent_id, state="AI_ACTIVE",
    ))
    db.commit()
    return general.id, conversation.id, inbound.id, agent_id, first_id


def test_geral_comercial_ignora_qualquer_vinculo_automatico(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            general_id, conversation_id, inbound_id, agent_id, _ = _case(db)
            queue = db.get(ServiceQueue, general_id)
            assert is_general_commercial_queue(queue)
            assert is_general_commercial_queue(SimpleNamespace(name="Comercial Geral", code="X"))

            for manual in ("true", "false"):
                monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", manual)
                if manual == "false":
                    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
                else:
                    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "1")
                assert not ai_queue_enabled(db, general_id)
                assert not ai_allowed_for_conversation(db, conversation_id)
                assert binding_for_conversation(db, conversation_id) is None
                assert usable_ai_binding(db, general_id) is None
                assert enqueue_ai_job(
                    db, conversation=db.get(Conversation, conversation_id),
                    message=db.get(Message, inbound_id),
                ) is None

            agent = db.get(AIAgentProfile, agent_id)
            conversation = db.get(Conversation, conversation_id)
            with pytest.raises(ValueError, match="Geral Comercial"):
                queue_ai_reply(
                    db, conversation=conversation, agent=agent,
                    inbound_id=inbound_id, text="Resposta indevida",
                )
            with pytest.raises(AIConfigurationError):
                _send_autonomous_reply(
                    db, agent=agent,
                    job=SimpleNamespace(conversation_id=conversation_id, message_id=inbound_id),
                    decision=SimpleNamespace(intent="GENERAL"),
                )
            with pytest.raises(CRMIntegrationError):
                _assert_closer_owns_conversation(db, conversation=conversation, agent=agent)
    finally:
        engine.dispose()


def test_limpeza_emergencial_cancela_jobs_followups_e_libera_a_geral(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            general_id, conversation_id, inbound_id, agent_id, _ = _case(db)
            db.add(AIJob(
                queue_id=general_id, agent_id=agent_id,
                conversation_id=conversation_id, message_id=inbound_id,
                status="PENDENTE", available_at=utcnow(),
            ))
            db.add(AIFollowupJob(
                queue_id=general_id, agent_id=agent_id,
                conversation_id=conversation_id, source_message_id=inbound_id,
                status="PENDENTE", due_at=utcnow()+timedelta(minutes=1),
            ))
            db.commit()
            result = enforce_general_commercial_human_only(db)
            db.commit()
            assert result["bindings"] == 1
            assert result["jobs"] == 1
            assert result["followups"] == 1
            assert result["released"] == 1
            assert db.scalar(select(AIJob.status).where(AIJob.conversation_id == conversation_id)) == "CANCELADO"
            assert db.scalar(select(AIFollowupJob.status).where(
                AIFollowupJob.conversation_id == conversation_id,
            )) == "CANCELADO"
            assert db.scalar(select(AIConversationState.state).where(
                AIConversationState.conversation_id == conversation_id,
            )) == "HUMAN_ACTIVE"
            conversation = db.get(Conversation, conversation_id)
            assert conversation.assigned_user_id is None
            assert conversation.status == "EM_FILA"
            assert enforce_general_commercial_human_only(db)["released"] == 0
    finally:
        engine.dispose()


def test_transferencia_manual_para_fila_ia_continua_permitida(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            general_id, conversation_id, _, agent_id, ai_queue_id = _case(db)
            ai_queue = db.get(ServiceQueue, ai_queue_id)
            ai_queue.name = "Fila IA"
            ai_queue.code = "FILA-IA"
            binding = db.scalar(select(AIQueueBinding).where(
                AIQueueBinding.queue_id == ai_queue_id
            ))
            binding.handling_mode = "AI_FIRST"
            binding.active = True
            binding.manual_authorized = True
            db.commit()
            assert not ai_allowed_for_conversation(db, conversation_id)
            bind_conversation_queue(
                db, conversation_id, ai_queue_id, origin=MANUAL_AI_ORIGIN,
            )
            assert ai_allowed_for_conversation(db, conversation_id)
            assert usable_ai_binding(db, ai_queue_id) is not None
            enforce_general_commercial_human_only(db)
            assert ai_allowed_for_conversation(db, conversation_id)
    finally:
        engine.dispose()
