"""IA opera exclusivamente na Fila IA, outras filas permanecem humanas."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIQueueBinding,
    AuditLog,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
)
from app.services.ai_queue_coverage import ensure_unanswered_coverage_all_queues
from app.services.ai_queue_scope import (
    ai_allowed_for_conversation,
    ai_allowed_for_queue,
    enforce_exclusive_ai_queue,
    exclusive_ai_queue_id,
)
from app.services.ai_sdr import binding_for_conversation, enqueue_ai_job, process_ai_job
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database, _setup


def _create_message(db, queue_id, agent_id, phone, *, assign_to_ai=False):
    contact = Contact(name="Cliente existente", phone=phone)
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id,
        source="WHATSAPP",
        team="DIGITAL",
        status="EM_ATENDIMENTO" if assign_to_ai else "EM_FILA",
        assigned_user_id=agent_id if assign_to_ai else None,
    )
    db.add(conversation)
    db.flush()
    db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue_id))
    message = Message(
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction="INBOUND",
        message_type="TEXT",
        status="RECEIVED",
        body="Olá, quero atendimento",
    )
    db.add(message)
    db.flush()
    return conversation, message


def test_exclusividade_desliga_outros_vinculos_e_pendencias_sem_apagar_historico(monkeypatch):
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, allowed_id, human_id, _ = _setup(db)
            assert allowed_id == 1
            db.get(ServiceQueue, allowed_id).name = "Fila IA"
            ensure_unanswered_coverage_all_queues(db)
            from app.models import AIAgentProfile
            agent_user_id = db.get(AIAgentProfile, agent_id).user_id

            human_conv, human_msg = _create_message(
                db, human_id, agent_user_id, "5555999998801", assign_to_ai=True
            )
            active_conv, active_msg = _create_message(
                db, allowed_id, agent_user_id, "5555999998802"
            )
            human_conv_id, human_msg_id = human_conv.id, human_msg.id
            active_conv_id, active_msg_id = active_conv.id, active_msg.id
            db.add(
                AIConversationState(
                    conversation_id=human_conv_id, agent_id=agent_id, state="AI_ACTIVE"
                )
            )
            db.add_all(
                [
                    AIJob(
                        queue_id=human_id,
                        agent_id=agent_id,
                        conversation_id=human_conv_id,
                        message_id=human_msg_id,
                        status="PENDENTE",
                    ),
                    AIJob(
                        queue_id=allowed_id,
                        agent_id=agent_id,
                        conversation_id=active_conv_id,
                        message_id=active_msg_id,
                        status="PENDENTE",
                    ),
                    AIFollowupJob(
                        queue_id=human_id,
                        agent_id=agent_id,
                        conversation_id=human_conv_id,
                        source_message_id=human_msg_id,
                        status="PENDENTE",
                        due_at=utcnow() + timedelta(minutes=15),
                    ),
                ]
            )
            db.commit()
            monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "1")
            assert exclusive_ai_queue_id() == 1
            assert ai_allowed_for_queue(1)
            assert not ai_allowed_for_queue(human_id)
            assert not ai_allowed_for_conversation(db, human_conv_id)
            assert ai_allowed_for_conversation(db, active_conv_id)
            result = enforce_exclusive_ai_queue(db)
            db.commit()
            assert result["target_ready"] is True
            assert human_id in result["bindings_removed"]
            assert result["jobs_cancelled"] == 1
            assert result["followups_cancelled"] == 1
            assert result["conversations_released"] == 1
            bindings = list(db.scalars(select(AIQueueBinding)))
            assert len(bindings) == 1
            assert bindings[0].queue_id == allowed_id
            assert bindings[0].active is True
            assert bindings[0].handling_mode != "HUMAN"
            human = db.get(Conversation, human_conv_id)
            assert human.status == "EM_FILA"
            assert human.assigned_user_id is None
            assert db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == human_conv_id
                )
            ).state == "HUMAN_ACTIVE"
            assert db.scalar(
                select(AIJob).where(AIJob.message_id == human_msg_id)
            ).status == "CANCELADO"
            assert db.scalar(
                select(AIJob).where(AIJob.message_id == active_msg_id)
            ).status == "PENDENTE"
            followup = db.scalar(
                select(AIFollowupJob).where(
                    AIFollowupJob.conversation_id == human_conv_id
                )
            )
            assert followup.status == "CANCELADO"
            assert db.get(Message, human_msg_id) is not None
            assert binding_for_conversation(db, human_conv_id) is None
            assert binding_for_conversation(db, active_conv_id) is not None
            assert enqueue_ai_job(db, conversation=human, message=db.get(Message, human_msg_id)) is None
            cancelled = db.scalar(
                select(AIJob).where(AIJob.message_id == human_msg_id)
            )
            assert process_ai_job(db, cancelled) == "skipped"
            assert cancelled.status == "CANCELADO"
            second = enforce_exclusive_ai_queue(db)
            db.commit()
            assert second["bindings_removed"] == []
            assert second["jobs_cancelled"] == 0
            assert second["followups_cancelled"] == 0
            assert second["conversations_released"] == 0
            assert db.scalar(
                select(AuditLog.id).where(
                    AuditLog.action == "AI_EXCLUSIVE_QUEUE_ENFORCED"
                )
            )
    finally:
        engine.dispose()


def test_cobertura_de_todas_filas_nao_reativa_as_outras(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            _, allowed_id, human_id, _ = _setup(db)
            monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", str(allowed_id))
            result = ensure_unanswered_coverage_all_queues(db)
            assert human_id not in result["enabled"]
            assert allowed_id in result["enabled"]
            assert db.scalar(
                select(AIQueueBinding).where(AIQueueBinding.queue_id == human_id)
            ) is None
    finally:
        engine.dispose()


def test_escopo_invalido_falha_fechado(monkeypatch):
    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "invalido")
    assert exclusive_ai_queue_id() == 0
    assert not ai_allowed_for_queue(1)
    assert not ai_allowed_for_queue(4)
