from __future__ import annotations

import json
import logging
import time
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AIAgentProfile,
    AIAutocorrectionEvent,
    AIClosingState,
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIQueueBinding,
    Conversation,
    Message,
)
from app.services.audit import write_audit
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_autocorrector")
AUTOCORRECT_ACTION = "AI_AUTOCORRECT_APPLIED"

_SAFE_ERROR_CUES = (
    "guardiao bloqueou",
    "integridade da jornada",
    "timeout",
    "tempor",
    "connection",
    "conexao",
    "502",
    "503",
    "504",
    "crm",
)


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )
    return " ".join(text.casefold().split())


def _is_safe_retriable_error(value: str | None) -> bool:
    folded = _fold(value)
    return bool(folded) and any(cue in folded for cue in _SAFE_ERROR_CUES)


def _already_applied(
    db: Session,
    *,
    source_type: str,
    source_id: int,
    action: str,
    hours: int = 6,
) -> bool:
    threshold = utcnow() - timedelta(hours=hours)
    return (
        db.scalar(
            select(AIAutocorrectionEvent.id).where(
                AIAutocorrectionEvent.source_type == source_type,
                AIAutocorrectionEvent.source_id == source_id,
                AIAutocorrectionEvent.action == action,
                AIAutocorrectionEvent.created_at >= threshold,
            ).limit(1)
        )
        is not None
    )


def _record(
    db: Session,
    *,
    conversation_id: int | None,
    source_type: str,
    source_id: int | None,
    action: str,
    reason_code: str,
    before: dict,
    after: dict,
    detail: str = "",
) -> None:
    db.add(
        AIAutocorrectionEvent(
            conversation_id=conversation_id,
            source_type=source_type,
            source_id=source_id,
            action=action,
            reason_code=reason_code,
            status="APPLIED",
            before_json=json.dumps(before, ensure_ascii=False, default=str),
            after_json=json.dumps(after, ensure_ascii=False, default=str),
            detail=detail[:2000],
        )
    )
    write_audit(
        db,
        user=None,
        action=AUTOCORRECT_ACTION,
        entity_type=source_type,
        entity_id=source_id,
        payload={
            "conversation_id": conversation_id,
            "action": action,
            "reason_code": reason_code,
            "before": before,
            "after": after,
            "detail": detail[:800],
        },
    )


def _repair_failed_jobs(db: Session, *, limit: int = 40) -> int:
    repaired = 0
    threshold = utcnow() - timedelta(hours=12)
    jobs = list(
        db.scalars(
            select(AIJob)
            .where(
                AIJob.status == "FALHOU",
                AIJob.updated_at >= threshold,
            )
            .order_by(AIJob.updated_at.asc())
            .limit(limit)
        )
    )
    for job in jobs:
        if not _is_safe_retriable_error(job.error):
            continue
        if _already_applied(
            db,
            source_type="ai_job",
            source_id=job.id,
            action="REQUEUE_SAFE_FAILURE",
        ):
            continue
        conversation = db.get(Conversation, job.conversation_id)
        if not conversation or conversation.status in {"BLOQUEADO", "RESOLVIDO"}:
            continue

        before = {
            "status": job.status,
            "attempts": job.attempts,
            "agent_id": job.agent_id,
            "error": job.error,
        }

        agent = db.get(AIAgentProfile, job.agent_id)
        if not agent or not agent.active:
            binding = db.scalar(
                select(AIQueueBinding).where(
                    AIQueueBinding.queue_id == job.queue_id,
                    AIQueueBinding.active.is_(True),
                )
            )
            if binding:
                replacement = db.get(AIAgentProfile, binding.agent_id)
                if replacement and replacement.active:
                    job.agent_id = replacement.id

        job.status = "PENDENTE"
        job.attempts = 0
        job.available_at = utcnow()
        job.locked_at = None
        job.error = None
        after = {
            "status": job.status,
            "attempts": job.attempts,
            "agent_id": job.agent_id,
        }
        _record(
            db,
            conversation_id=job.conversation_id,
            source_type="ai_job",
            source_id=job.id,
            action="REQUEUE_SAFE_FAILURE",
            reason_code="SAFE_RETRIABLE_FAILURE",
            before=before,
            after=after,
        )
        repaired += 1
    return repaired


def _repair_failed_followups(db: Session, *, limit: int = 40) -> int:
    repaired = 0
    threshold = utcnow() - timedelta(hours=12)
    jobs = list(
        db.scalars(
            select(AIFollowupJob)
            .where(
                AIFollowupJob.status == "FALHOU",
                AIFollowupJob.updated_at >= threshold,
            )
            .order_by(AIFollowupJob.updated_at.asc())
            .limit(limit)
        )
    )
    for job in jobs:
        if not _is_safe_retriable_error(job.error):
            continue
        if _already_applied(
            db,
            source_type="ai_followup_job",
            source_id=job.id,
            action="RESCHEDULE_SAFE_FAILURE",
        ):
            continue
        conversation = db.get(Conversation, job.conversation_id)
        if (
            not conversation
            or conversation.status in {"BLOQUEADO", "RESOLVIDO", "ENCERRADO"}
        ):
            continue

        before = {
            "status": job.status,
            "attempts": job.attempts,
            "due_at": job.due_at,
            "error": job.error,
        }
        job.status = "PENDENTE"
        job.attempts = 0
        job.due_at = utcnow() + timedelta(minutes=5)
        job.locked_at = None
        job.error = None
        after = {
            "status": job.status,
            "attempts": job.attempts,
            "due_at": job.due_at,
        }
        _record(
            db,
            conversation_id=job.conversation_id,
            source_type="ai_followup_job",
            source_id=job.id,
            action="RESCHEDULE_SAFE_FAILURE",
            reason_code="SAFE_RETRIABLE_FAILURE",
            before=before,
            after=after,
        )
        repaired += 1
    return repaired


def _repair_closing_ownership(db: Session, *, limit: int = 80) -> int:
    repaired = 0
    closings = list(
        db.scalars(
            select(AIClosingState)
            .where(
                AIClosingState.step.not_in(["COMPLETED", "ESCALATED"]),
                AIClosingState.updated_at >= utcnow() - timedelta(days=7),
            )
            .order_by(AIClosingState.updated_at.desc())
            .limit(limit)
        )
    )
    for closing in closings:
        conversation = db.get(Conversation, closing.conversation_id)
        closer = db.get(AIAgentProfile, closing.agent_id)
        if (
            not conversation
            or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
            or not closer
            or not closer.active
            or closer.role_type != "CLOSER"
        ):
            continue

        state = db.scalar(
            select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id
            )
        )
        consistent = bool(
            state
            and state.state == "CLOSING"
            and int(state.agent_id or 0) == int(closing.agent_id or 0)
            and int(conversation.assigned_user_id or 0) == int(closer.user_id or 0)
        )
        if consistent:
            continue

        if _already_applied(
            db,
            source_type="conversation",
            source_id=conversation.id,
            action="REPAIR_CLOSER_OWNERSHIP",
            hours=2,
        ):
            continue

        before = {
            "ai_state": state.state if state else None,
            "ai_agent_id": state.agent_id if state else None,
            "assigned_user_id": conversation.assigned_user_id,
            "closing_agent_id": closing.agent_id,
            "closing_step": closing.step,
        }

        if not state:
            state = AIConversationState(
                conversation_id=conversation.id,
                agent_id=closing.agent_id,
                state="CLOSING",
                summary=closing.handoff_summary or "",
                qualification_score=100,
                temperature="QUENTE",
                last_intent="PURCHASE_INTENT",
                handoff_at=utcnow(),
            )
            db.add(state)
        else:
            state.agent_id = closing.agent_id
            state.state = "CLOSING"
            state.last_intent = "PURCHASE_INTENT"
            state.handoff_at = state.handoff_at or utcnow()

        conversation.assigned_user_id = closer.user_id
        conversation.status = "EM_ATENDIMENTO"

        stale_followups = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
                    AIFollowupJob.agent_id != closing.agent_id,
                )
            )
        )
        for followup in stale_followups:
            followup.status = "CANCELADO"
            followup.locked_at = None
            followup.error = "OWNERSHIP_MOVED_TO_CLOSER"

        after = {
            "ai_state": "CLOSING",
            "ai_agent_id": closing.agent_id,
            "assigned_user_id": closer.user_id,
            "cancelled_stale_followups": len(stale_followups),
        }
        _record(
            db,
            conversation_id=conversation.id,
            source_type="conversation",
            source_id=conversation.id,
            action="REPAIR_CLOSER_OWNERSHIP",
            reason_code="CLOSING_STATE_OWNER_MISMATCH",
            before=before,
            after=after,
        )
        repaired += 1

    return repaired


def _repair_missed_purchase_intent(db: Session, *, limit: int = 80) -> int:
    repaired = 0
    states = list(
        db.scalars(
            select(AIConversationState)
            .where(
                AIConversationState.state == "AI_ACTIVE",
                AIConversationState.updated_at >= utcnow() - timedelta(days=7),
            )
            .order_by(AIConversationState.updated_at.desc())
            .limit(limit)
        )
    )
    from app.services.ai_sdr import _explicit_purchase_commitment, enqueue_ai_job

    for state in states:
        conversation = db.get(Conversation, state.conversation_id)
        owner = db.get(AIAgentProfile, state.agent_id)
        if (
            not conversation
            or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
            or not owner
            or owner.role_type == "CLOSER"
        ):
            continue

        closing_exists = db.scalar(
            select(AIClosingState.id).where(
                AIClosingState.conversation_id == conversation.id
            ).limit(1)
        )
        if closing_exists:
            continue

        latest_inbound = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.direction == "INBOUND",
                Message.message_type == "TEXT",
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        if not latest_inbound or not _explicit_purchase_commitment(
            {"latest_customer_message": latest_inbound.body or ""}
        ):
            continue

        if _already_applied(
            db,
            source_type="message",
            source_id=latest_inbound.id,
            action="REPROCESS_MISSED_PURCHASE_INTENT",
            hours=72,
        ):
            continue

        pending_followups = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
                )
            )
        )
        for followup in pending_followups:
            followup.status = "CANCELADO"
            followup.locked_at = None
            followup.error = "MISSED_PURCHASE_INTENT_REPAIR"

        job = db.scalar(
            select(AIJob).where(AIJob.message_id == latest_inbound.id).limit(1)
        )
        before = {
            "message_id": latest_inbound.id,
            "job_id": job.id if job else None,
            "job_status": job.status if job else None,
            "cancelled_followups": len(pending_followups),
        }

        if job:
            job.agent_id = state.agent_id
            job.status = "PENDENTE"
            job.attempts = 0
            job.available_at = utcnow()
            job.locked_at = None
            job.error = None
        else:
            job = enqueue_ai_job(
                db,
                conversation=conversation,
                message=latest_inbound,
            )

        if not job:
            continue

        state.last_intent = "PURCHASE_INTENT"
        state.summary = (
            (state.summary or "").rstrip()
            + "\n[Autocorreção] Intenção explícita de compra reprocessada."
        ).strip()[-5000:]

        _record(
            db,
            conversation_id=conversation.id,
            source_type="message",
            source_id=latest_inbound.id,
            action="REPROCESS_MISSED_PURCHASE_INTENT",
            reason_code="PURCHASE_INTENT_STUCK_IN_SDR",
            before=before,
            after={
                "job_id": job.id,
                "job_status": job.status,
                "agent_id": job.agent_id,
                "followups_cancelled": len(pending_followups),
            },
        )
        repaired += 1

    return repaired


def _restore_replied_closed_conversations(db: Session, *, limit: int = 30) -> int:
    restored = 0
    conversations = list(
        db.scalars(
            select(Conversation)
            .where(
                Conversation.status.in_(["ENCERRADO", "RESOLVIDO"]),
                Conversation.closed_at.is_not(None),
                Conversation.updated_at >= utcnow() - timedelta(days=7),
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
    )
    for conversation in conversations:
        state = db.scalar(
            select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id
            )
        )
        if not state or state.state not in {"AI_ACTIVE", "CLOSING"}:
            continue

        latest = db.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.id.desc())
            .limit(1)
        )
        if (
            not latest
            or latest.direction != "INBOUND"
            or not conversation.closed_at
            or not latest.created_at
            or latest.created_at <= conversation.closed_at
        ):
            continue

        if _already_applied(
            db,
            source_type="conversation",
            source_id=conversation.id,
            action="RESTORE_AFTER_CUSTOMER_REPLY",
            hours=24,
        ):
            continue

        before = {
            "status": conversation.status,
            "closed_at": conversation.closed_at,
            "latest_message_id": latest.id,
        }
        conversation.status = (
            "EM_ATENDIMENTO" if conversation.assigned_user_id else "EM_FILA"
        )
        conversation.closed_at = None

        existing_job = db.scalar(
            select(AIJob.id).where(AIJob.message_id == latest.id).limit(1)
        )
        if not existing_job:
            from app.services.ai_sdr import enqueue_ai_job

            enqueue_ai_job(db, conversation=conversation, message=latest)

        after = {
            "status": conversation.status,
            "closed_at": None,
            "latest_message_id": latest.id,
        }
        _record(
            db,
            conversation_id=conversation.id,
            source_type="conversation",
            source_id=conversation.id,
            action="RESTORE_AFTER_CUSTOMER_REPLY",
            reason_code="CUSTOMER_REPLIED_AFTER_CLOSE",
            before=before,
            after=after,
        )
        restored += 1
    return restored


def run_autocorrection_cycle(db: Session) -> dict[str, int]:
    if not settings.ai_autocorrect_enabled:
        return {
            "jobs": 0,
            "followups": 0,
            "conversations": 0,
            "ownership": 0,
            "purchase_intent": 0,
        }

    result = {
        "ownership": _repair_closing_ownership(db),
        "purchase_intent": _repair_missed_purchase_intent(db),
        "jobs": _repair_failed_jobs(db),
        "followups": _repair_failed_followups(db),
        "conversations": _restore_replied_closed_conversations(db),
    }
    return result


def run_autocorrector_loop() -> None:
    poll = max(15, int(settings.ai_autocorrect_poll_seconds or 30))
    LOG.info("SulIA autocorrector iniciado. enabled=%s poll=%ss", settings.ai_autocorrect_enabled, poll)
    while True:
        if settings.ai_autocorrect_enabled:
            db = SessionLocal()
            try:
                result = run_autocorrection_cycle(db)
                db.commit()
                if any(result.values()):
                    LOG.info("ai_autocorrect_cycle=%s", result)
            except Exception:
                db.rollback()
                LOG.exception("Falha no ciclo de autocorreção operacional.")
            finally:
                db.close()
        time.sleep(poll)
