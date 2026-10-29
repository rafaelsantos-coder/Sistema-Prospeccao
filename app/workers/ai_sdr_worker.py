from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import AIFollowupJob, AIConversationState, AIJob, Conversation, Message
from app.services.ai_followups import claim_followup_job, process_followup_job
from app.services.ai_sdr import claim_ai_job, enqueue_ai_job, process_ai_job, queue_ai_binding
from app.services.queue_routing import bind_conversation_queue, queue_for_conversation
from app.services.zapi_instances import conversation_instance, default_queue_for_instance
from app.timeutils import utcnow


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_ai_sdr_worker")


def _recover_stale_jobs() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        threshold = utcnow() - timedelta(minutes=10)
        jobs = list(
            db.scalars(
                select(AIJob).where(
                    AIJob.status == "PROCESSANDO",
                    AIJob.locked_at.is_not(None),
                    AIJob.locked_at < threshold,
                )
            )
        )
        for job in jobs:
            job.status = "PENDENTE"
            job.locked_at = None
            job.available_at = utcnow()
            job.error = "Job recuperado após timeout do worker."
            recovered += 1
        db.commit()
    finally:
        db.close()
    return recovered


def _recover_stale_followups() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        threshold = utcnow() - timedelta(minutes=10)
        jobs = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.status == "PROCESSANDO",
                    AIFollowupJob.locked_at.is_not(None),
                    AIFollowupJob.locked_at < threshold,
                )
            )
        )
        for job in jobs:
            job.status = "PENDENTE"
            job.locked_at = None
            job.due_at = utcnow()
            job.error = "Follow-up recuperado após timeout do worker."
            recovered += 1
        db.commit()
    finally:
        db.close()
    return recovered


def _recover_recent_unqueued_inbounds() -> int:
    db = SessionLocal()
    recovered = 0
    reasons: Counter[str] = Counter()
    inspected: list[str] = []
    try:
        threshold = utcnow() - timedelta(minutes=30)
        messages = list(
            db.scalars(
                select(Message)
                .where(
                    Message.direction == "INBOUND",
                    Message.message_type == "TEXT",
                    Message.created_at >= threshold,
                )
                .order_by(Message.id.desc())
                .limit(200)
            )
        )
        reasons["recent_text"] = len(messages)
        seen_conversations: set[int] = set()
        for message in messages:
            if message.conversation_id in seen_conversations:
                reasons["older_same_conversation"] += 1
                continue
            seen_conversations.add(message.conversation_id)

            existing_job = db.scalar(
                select(AIJob.id).where(AIJob.message_id == message.id).limit(1)
            )
            if existing_job:
                reasons["already_has_job"] += 1
                continue

            conversation = db.get(Conversation, message.conversation_id)
            if not conversation:
                reasons["missing_conversation"] += 1
                continue
            if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
                reasons["closed_conversation"] += 1
                continue

            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id
                )
            )
            if state and state.state in {"HANDOFF", "HUMAN_ACTIVE", "OPT_OUT"}:
                reasons[f"state_{state.state.lower()}"] += 1
                continue

            later_outbound = db.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == "OUTBOUND",
                    Message.id > message.id,
                )
                .order_by(Message.id.asc())
                .limit(1)
            )
            if later_outbound:
                reasons["later_outbound"] += 1
                continue

            current_queue = queue_for_conversation(db, conversation.id)
            instance = conversation_instance(db, conversation.id)
            target_queue = default_queue_for_instance(db, instance.id) if instance else None
            target_binding = queue_ai_binding(db, target_queue.id) if target_queue else None

            if len(inspected) < 12:
                inspected.append(
                    "conversation=%s message=%s status=%s current_queue=%s instance=%s target_queue=%s target_ai=%s"
                    % (
                        conversation.id,
                        message.id,
                        conversation.status,
                        current_queue.id if current_queue else None,
                        instance.id if instance else None,
                        target_queue.id if target_queue else None,
                        bool(target_binding),
                    )
                )

            if instance and target_queue and target_binding:
                if not current_queue or current_queue.id != target_queue.id:
                    bind_conversation_queue(
                        db,
                        conversation.id,
                        target_queue.id,
                        origin="AI_RECOVERY_INSTANCE",
                    )
                    conversation.team = target_queue.team
                    if conversation.assigned_user_id:
                        conversation.assigned_user_id = None
                        conversation.status = "EM_FILA"
                    reasons["queue_rebound"] += 1
            elif not instance:
                reasons["no_instance"] += 1
            elif not target_queue:
                reasons["no_target_queue"] += 1
            elif not target_binding:
                reasons["target_without_ai"] += 1

            job = enqueue_ai_job(
                db,
                conversation=conversation,
                message=message,
            )
            if job:
                recovered += 1
                reasons["recovered"] += 1
            else:
                reasons["enqueue_skipped"] += 1

        db.commit()
        LOG.info("AI inbound recovery diagnostics: %s", dict(reasons))
        for row in inspected:
            LOG.info("AI inbound recovery candidate: %s", row)
    finally:
        db.close()
    return recovered


def run() -> None:
    Base.metadata.create_all(bind=engine)
    poll = max(2, int(settings.ai_sdr_poll_seconds or 3))
    LOG.info(
        "SulIA SDR worker iniciado. enabled=%s openai_configured=%s poll=%ss",
        settings.ai_sdr_enabled,
        bool(settings.openai_api_key),
        poll,
    )
    _recover_stale_jobs()
    _recover_stale_followups()
    recovered_recent = _recover_recent_unqueued_inbounds()
    if recovered_recent:
        LOG.info("Mensagens recentes sem job recuperadas=%s", recovered_recent)

    missing_key_logged = False
    while True:
        if not settings.ai_sdr_enabled:
            time.sleep(max(10, poll))
            continue
        if not settings.openai_api_key:
            if not missing_key_logged:
                LOG.warning(
                    "OPENAI_API_KEY ausente. Shadow jobs permanecerão pendentes até a configuração da chave."
                )
                missing_key_logged = True
            time.sleep(max(15, poll))
            continue
        missing_key_logged = False

        db = SessionLocal()
        job_id = None
        followup_id = None
        try:
            job = claim_ai_job(db)
            if job:
                job_id = job.id
                db.commit()
                job = db.get(AIJob, job_id)
                if job:
                    outcome = process_ai_job(db, job)
                    db.commit()
                    LOG.info(
                        "ai_job=%s conversation=%s agent=%s outcome=%s attempts=%s",
                        job.id,
                        job.conversation_id,
                        job.agent_id,
                        outcome,
                        job.attempts,
                    )
            else:
                followup = claim_followup_job(db)
                if not followup:
                    db.commit()
                else:
                    followup_id = followup.id
                    db.commit()
                    followup = db.get(AIFollowupJob, followup_id)
                    if followup:
                        outcome = process_followup_job(db, followup)
                        db.commit()
                        LOG.info(
                            "ai_followup=%s conversation=%s step=%s outcome=%s attempts=%s",
                            followup.id,
                            followup.conversation_id,
                            followup.step,
                            outcome,
                            followup.attempts,
                        )
        except Exception:
            db.rollback()
            LOG.exception(
                "Falha inesperada no SulIA SDR worker job=%s followup=%s",
                job_id,
                followup_id,
            )
        finally:
            db.close()

        time.sleep(poll)


if __name__ == "__main__":
    run()
