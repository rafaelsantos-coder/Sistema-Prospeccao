from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import AIAgentProfile, AIFollowupJob, AIClosingState, AIConversationState, AIJob, AIQueueBinding, AIRun, Conversation, Message
from app.services.ai_followups import claim_followup_job, process_followup_job, schedule_followup
from app.services.ai_sdr import claim_ai_job, enqueue_ai_job, process_ai_job, queue_ai_binding
from app.services.ai_seller import (
    active_closer,
    poll_pending_contracts,
    resume_simplified_mobile_states,
)
from app.services.ai_supervisor import run_shadow_supervisor_loop
from app.services.ai_autocorrector import run_autocorrector_loop
from app.services.ai_learning import run_learning_loop
from app.services.ai_engineer import run_engineer_loop
from app.services.ai_weekly_analyst import run_weekly_analyst_loop
from app.services.ai_sdr_policy import followup_spec, next_business_time
from app.services.queue_routing import bind_conversation_queue, queue_for_conversation
from app.services.zapi_instances import conversation_instance, default_queue_for_instance
from app.timeutils import utc_naive, utcnow


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_ai_sdr_worker")


def _real_mode_cutoff():
    raw = os.getenv("AI_REAL_MODE_SINCE", "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        LOG.error("AI_REAL_MODE_SINCE inválido: %s", raw)
        return None


def _cancel_pre_real_mode_work() -> tuple[int, int]:
    cutoff = _real_mode_cutoff()
    if not cutoff:
        return 0, 0
    db = SessionLocal()
    cancelled_jobs = 0
    cancelled_followups = 0
    try:
        jobs = list(db.scalars(select(AIJob).where(
            AIJob.status.in_(["PENDENTE", "PROCESSANDO"]),
            AIJob.created_at < cutoff,
        )))
        for job in jobs:
            job.status = "CANCELADO"
            job.locked_at = None
            job.error = "Cancelado pelo marco de início dos casos reais."
            cancelled_jobs += 1

        followups = list(db.scalars(select(AIFollowupJob).where(
            AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
            AIFollowupJob.created_at < cutoff,
        )))
        for job in followups:
            job.status = "CANCELADO"
            job.locked_at = None
            job.error = "Cancelado pelo marco de início dos casos reais."
            cancelled_followups += 1
        db.commit()
    finally:
        db.close()
    return cancelled_jobs, cancelled_followups


def _ensure_bound_ai_agents_active() -> int:
    if os.getenv("AI_ENSURE_BOUND_AGENTS_ACTIVE", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return 0
    db = SessionLocal()
    activated = 0
    try:
        bindings = list(db.scalars(select(AIQueueBinding).where(
            AIQueueBinding.active.is_(True),
            AIQueueBinding.handling_mode != "HUMAN",
        )))
        for binding in bindings:
            agent = db.get(AIAgentProfile, binding.agent_id)
            if agent and not agent.active:
                agent.active = True
                activated += 1
        db.commit()
    finally:
        db.close()
    return activated


def _reset_legacy_state_for_real_inbound(db, conversation: Conversation, message: Message) -> bool:
    cutoff = _real_mode_cutoff()
    message_created_at = utc_naive(message.created_at)
    if not cutoff or not message_created_at or message_created_at < cutoff:
        return False

    state = db.scalar(select(AIConversationState).where(
        AIConversationState.conversation_id == conversation.id
    ))
    closing = db.scalar(select(AIClosingState).where(
        AIClosingState.conversation_id == conversation.id
    ))

    legacy_state = bool(
        (state and state.created_at and utc_naive(state.created_at) < cutoff)
        or (closing and closing.created_at and utc_naive(closing.created_at) < cutoff)
    )
    if not legacy_state:
        return False

    for followup in db.scalars(select(AIFollowupJob).where(
        AIFollowupJob.conversation_id == conversation.id,
        AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
    )):
        followup.status = "CANCELADO"
        followup.locked_at = None
        followup.error = "Cancelado ao reiniciar conversa como caso real."

    if closing:
        db.delete(closing)

    if state:
        state.state = "AI_ACTIVE"
        state.summary = ""
        state.qualification_score = 0
        state.temperature = "FRIO"
        state.last_intent = "UNKNOWN"
        state.last_run_id = None
        state.handoff_at = None

    conversation.assigned_user_id = None
    conversation.status = "EM_FILA"
    db.flush()
    LOG.warning(
        "real_case_state_reset conversation=%s message=%s cutoff=%s",
        conversation.id,
        message.id,
        cutoff,
    )
    return True


def _recover_stale_jobs() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        threshold = utcnow() - timedelta(minutes=10)
        cutoff = _real_mode_cutoff()
        jobs = list(
            db.scalars(
                select(AIJob).where(
                    AIJob.status == "PROCESSANDO",
                    AIJob.locked_at.is_not(None),
                    AIJob.locked_at < threshold,
                    *( [AIJob.created_at >= cutoff] if cutoff else [] ),
                )
            )
        )
        for job in jobs:
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == job.conversation_id
                )
            )
            agent = db.get(AIAgentProfile, job.agent_id)
            invalid_owner = bool(
                state
                and (
                    (
                        state.state == "CLOSING"
                        and (
                            not agent
                            or agent.role_type != "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                    or (
                        state.state == "AI_ACTIVE"
                        and (
                            not agent
                            or agent.role_type == "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                )
            )
            if invalid_owner:
                job.status = "IGNORADO"
                job.locked_at = None
                job.error = "Job antigo invalidado por mudança de ownership."
                continue
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
        cutoff = _real_mode_cutoff()
        jobs = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.status == "PROCESSANDO",
                    AIFollowupJob.locked_at.is_not(None),
                    AIFollowupJob.locked_at < threshold,
                    *( [AIFollowupJob.created_at >= cutoff] if cutoff else [] ),
                )
            )
        )
        for job in jobs:
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == job.conversation_id
                )
            )
            agent = db.get(AIAgentProfile, job.agent_id)
            invalid_owner = bool(
                state
                and (
                    (
                        state.state == "CLOSING"
                        and (
                            not agent
                            or agent.role_type != "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                    or (
                        state.state == "AI_ACTIVE"
                        and (
                            not agent
                            or agent.role_type == "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                )
            )
            if invalid_owner:
                job.status = "CANCELADO"
                job.locked_at = None
                job.error = "Follow-up antigo invalidado por mudança de ownership."
                continue
            job.status = "PENDENTE"
            job.locked_at = None
            job.due_at = utcnow()
            job.error = "Follow-up recuperado após timeout do worker."
            recovered += 1
        db.commit()
    finally:
        db.close()
    return recovered


def _reschedule_initial_pending_followups() -> int:
    db = SessionLocal()
    updated = 0
    try:
        now = utcnow()
        cutoff = _real_mode_cutoff()
        jobs = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.status == "PENDENTE",
                    AIFollowupJob.step <= 6,
                    *( [AIFollowupJob.created_at >= cutoff] if cutoff else [] ),
                )
            )
        )
        for job in jobs:
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == job.conversation_id
                )
            )
            agent = db.get(AIAgentProfile, job.agent_id)
            invalid_owner = bool(
                state
                and (
                    (
                        state.state == "CLOSING"
                        and (
                            not agent
                            or agent.role_type != "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                    or (
                        state.state == "AI_ACTIVE"
                        and (
                            not agent
                            or agent.role_type == "CLOSER"
                            or int(state.agent_id or 0) != int(job.agent_id)
                        )
                    )
                )
            )
            if invalid_owner:
                job.status = "CANCELADO"
                job.locked_at = None
                job.error = "Follow-up invalidado por mudança de ownership."
                continue
            delay, theme = followup_spec(int(job.step or 1))
            job.theme = theme
            job.due_at = next_business_time(
                now + delay,
                settings.default_timezone,
            )
            job.locked_at = None
            updated += 1
        db.commit()
    finally:
        db.close()
    return updated


def _recover_closer_capture_patch_failures() -> int:
    """Recupera escalonamentos técnicos antigos sem adivinhar a etapa."""
    db = SessionLocal()
    recovered = 0
    try:
        closer = active_closer(db)
        cutoff = _real_mode_cutoff()
        if not closer:
            return 0

        states = list(
            db.scalars(
                select(AIClosingState)
                .where(
                    AIClosingState.step == "ESCALATED",
                    AIClosingState.last_error.ilike("%Falha repetida na integração CRM:%"),
                    AIClosingState.updated_at >= max(utcnow() - timedelta(hours=12), cutoff) if cutoff else AIClosingState.updated_at >= utcnow() - timedelta(hours=12),
                )
                .order_by(AIClosingState.updated_at.desc())
                .limit(50)
            )
        )
        for state in states:
            conversation = db.get(Conversation, state.conversation_id)
            if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
                continue

            # A fonte da verdade é o snapshot do último run que falhou. Ele guarda
            # a etapa antes da tentativa, evitando voltar para NAME/CPF/BILLING.
            failed_run = db.scalar(
                select(AIRun)
                .where(
                    AIRun.conversation_id == conversation.id,
                    AIRun.agent_id == closer.id,
                    AIRun.status == "FALHOU",
                )
                .order_by(AIRun.id.desc())
                .limit(1)
            )
            resume_step = ""
            if failed_run:
                try:
                    import json
                    snapshot = json.loads(failed_run.input_snapshot_json or "{}")
                    resume_step = str(snapshot.get("step") or "").strip()
                except Exception:
                    resume_step = ""

            if not resume_step or resume_step in {"ESCALATED", "COMPLETED"}:
                LOG.warning(
                    "closer_crm_recovery_skipped_no_exact_step conversation=%s",
                    conversation.id,
                )
                continue

            state.step = resume_step
            state.agent_id = closer.id
            state.last_error = ""
            conversation.assigned_user_id = closer.user_id
            conversation.status = "EM_ATENDIMENTO"
            if conversation.lead:
                conversation.lead.assigned_user_id = closer.user_id

            ai_state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id
                )
            )
            if ai_state:
                ai_state.agent_id = closer.id
                ai_state.state = "CLOSING"

            failed_job = db.scalar(
                select(AIJob)
                .where(
                    AIJob.conversation_id == conversation.id,
                    AIJob.agent_id == closer.id,
                    AIJob.status == "FALHOU",
                )
                .order_by(AIJob.id.desc())
                .limit(1)
            )
            if not failed_job:
                LOG.warning(
                    "closer_crm_recovery_without_failed_job conversation=%s",
                    conversation.id,
                )
                continue

            failed_job.agent_id = closer.id
            failed_job.status = "PENDENTE"
            failed_job.attempts = 0
            failed_job.available_at = utcnow()
            failed_job.locked_at = None
            failed_job.error = None
            recovered += 1

        db.commit()
        if states:
            LOG.info(
                "closer_crm_recovery_scan candidates=%s recovered=%s",
                len(states),
                recovered,
            )
    finally:
        db.close()
    return recovered

def _backfill_missing_followups(*, limit: int = 250) -> int:
    """Recria cadência para conversas IA ativas que ficaram sem job pendente."""
    db = SessionLocal()
    created = 0
    try:
        cutoff = _real_mode_cutoff()
        states = list(
            db.scalars(
                select(AIConversationState)
                .where(
                    AIConversationState.state.in_(["AI_ACTIVE", "CLOSING"]),
                    AIConversationState.updated_at >= max(utcnow() - timedelta(days=30), cutoff) if cutoff else AIConversationState.updated_at >= utcnow() - timedelta(days=30),
                )
                .order_by(AIConversationState.updated_at.desc())
                .limit(limit)
            )
        )
        for state in states:
            conversation = db.get(Conversation, state.conversation_id)
            if (
                not conversation
                or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
            ):
                continue

            if state.state == "CLOSING":
                from app.services.ai_seller import SELLER_CUSTOMER_INPUT_STEPS
                closing = db.scalar(
                    select(AIClosingState).where(
                        AIClosingState.conversation_id == conversation.id
                    )
                )
                owner = db.get(AIAgentProfile, state.agent_id)
                if (
                    not closing
                    or not owner
                    or owner.role_type != "CLOSER"
                    or int(closing.agent_id or 0) != int(state.agent_id or 0)
                ):
                    LOG.error(
                        "closing_owner_inconsistent conversation=%s ai_agent=%s closing_agent=%s",
                        conversation.id,
                        state.agent_id,
                        closing.agent_id if closing else None,
                    )
                    continue
                if closing.step not in SELLER_CUSTOMER_INPUT_STEPS:
                    # Crédito em análise, geração de contrato e outras etapas
                    # sistêmicas são tratadas pelos pollers, não por cobrança ao cliente.
                    continue

            pending = db.scalar(
                select(AIFollowupJob.id)
                .where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
                )
                .limit(1)
            )
            if pending:
                continue

            latest = db.scalar(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction.in_(["INBOUND", "OUTBOUND"]),
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            if not latest or latest.direction != "OUTBOUND":
                continue
            if (
                cutoff
                and latest.created_at
                and (utc_naive(latest.created_at) or latest.created_at) < cutoff
            ):
                continue

            source_inbound_id = db.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == "INBOUND",
                    Message.id < latest.id,
                )
                .order_by(Message.id.desc())
                .limit(1)
            )

            last_sent = db.scalar(
                select(AIFollowupJob)
                .where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.source_message_id == source_inbound_id,
                    AIFollowupJob.status == "ENVIADO",
                )
                .order_by(AIFollowupJob.step.desc(), AIFollowupJob.id.desc())
                .limit(1)
            )
            next_step = int(last_sent.step or 0) + 1 if last_sent else 1

            queue = queue_for_conversation(db, conversation.id)
            if not queue:
                continue
            job = schedule_followup(
                db,
                agent_id=state.agent_id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                source_message_id=source_inbound_id,
                step=next_step,
            )
            if job:
                delay, _theme = followup_spec(next_step)
                last_outbound_at = latest.created_at or utcnow()
                ideal_due = utc_naive(
                    next_business_time(
                        last_outbound_at + delay,
                        settings.default_timezone,
                    )
                ) or utcnow()
                # Conversas que já venceram a janela entram na próxima janela útil,
                # sem esperar mais um ciclo inteiro da cadência. Normalizamos para
                # UTC naive, padrão interno do app, porque o PostgreSQL pode devolver
                # created_at com tzinfo mesmo quando utcnow() é naive.
                now_utc = utcnow()
                next_window = utc_naive(
                    next_business_time(
                        now_utc,
                        settings.default_timezone,
                    )
                ) or now_utc
                job.due_at = next_window if ideal_due <= now_utc else ideal_due
                created += 1

        db.commit()
    finally:
        db.close()
    return created


def _recover_recent_unqueued_inbounds() -> int:
    db = SessionLocal()
    recovered = 0
    reasons: Counter[str] = Counter()
    inspected: list[str] = []
    try:
        cutoff = _real_mode_cutoff()
        # Só mensagem nova: a IA não vai atrás de mensagem antiga não lida na fila.
        threshold = utcnow() - timedelta(minutes=10)
        if cutoff and cutoff > threshold:
            threshold = cutoff
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

            _reset_legacy_state_for_real_inbound(db, conversation, message)

            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id
                )
            )
            # Estados de handoff humano não bloqueiam mais: enqueue_ai_job devolve
            # a conversa para a IA (não existe atendimento humano).
            if state and state.state == "OPT_OUT":
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

            enabled_at = utc_naive(getattr(target_binding, "updated_at", None)) if target_binding else None
            message_at = utc_naive(message.created_at)
            if enabled_at and message_at and message_at < enabled_at:
                # Chegou antes de a IA ser ligada na fila: fica para o vendedor.
                reasons["before_ai_enabled"] += 1
                continue

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


# Fila "IA quando não houver vendedor online": se o vendedor online não responder
# o cliente em 10 minutos, a IA assume (pedido do comercial, 05/11).
UNANSWERED_TAKEOVER_AFTER = timedelta(minutes=10)
UNANSWERED_TAKEOVER_WINDOW = timedelta(hours=3)
_TAKEOVER_BLOCKED_STATES = {
    "AI_ACTIVE",
    "CLOSING",
    "HANDOFF",
    "HANDOFF_HUMAN",
    "HUMAN_ACTIVE",
    "OPT_OUT",
    "ESCALATED",
    "COMPLETED",
}


def _takeover_unanswered_human_conversations(*, limit: int = 50) -> int:
    from app.models import ConversationAssignment, ConversationQueueLink
    from app.services.queue_routing import AI_WHEN_OFFLINE_MODES

    db = SessionLocal()
    taken = 0
    try:
        now = utcnow()
        bindings = list(
            db.scalars(
                select(AIQueueBinding).where(
                    AIQueueBinding.active.is_(True),
                    AIQueueBinding.handling_mode.in_(AI_WHEN_OFFLINE_MODES),
                )
            )
        )
        ai_users = {user_id for user_id in db.scalars(select(AIAgentProfile.user_id)) if user_id}
        for binding in bindings:
            enabled_at = utc_naive(binding.updated_at)
            conversations = list(
                db.scalars(
                    select(Conversation)
                    .join(ConversationQueueLink, ConversationQueueLink.conversation_id == Conversation.id)
                    .where(
                        ConversationQueueLink.queue_id == binding.queue_id,
                        Conversation.status.not_in(["RESOLVIDO", "ENCERRADO", "BLOQUEADO"]),
                        Conversation.updated_at >= now - UNANSWERED_TAKEOVER_WINDOW,
                    )
                    .order_by(Conversation.updated_at.desc())
                    .limit(limit)
                )
            )
            for conversation in conversations:
                state = db.scalar(
                    select(AIConversationState.state).where(
                        AIConversationState.conversation_id == conversation.id
                    )
                )
                if state in _TAKEOVER_BLOCKED_STATES:
                    continue
                latest = db.scalar(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.direction.in_(["INBOUND", "OUTBOUND"]),
                    )
                    .order_by(Message.id.desc())
                    .limit(1)
                )
                if not latest or latest.direction != "INBOUND" or latest.message_type != "TEXT":
                    continue
                received_at = utc_naive(latest.created_at)
                if not received_at or now - received_at < UNANSWERED_TAKEOVER_AFTER:
                    continue
                if now - received_at > UNANSWERED_TAKEOVER_WINDOW:
                    continue
                if enabled_at and received_at < enabled_at:
                    continue
                if db.scalar(select(AIJob.id).where(AIJob.message_id == latest.id).limit(1)):
                    continue
                previous_owner = conversation.assigned_user_id
                if previous_owner and previous_owner not in ai_users:
                    conversation.assigned_user_id = None
                    conversation.status = "EM_FILA"
                    db.add(
                        ConversationAssignment(
                            conversation_id=conversation.id,
                            user_id=None,
                            assigned_by_id=None,
                            event="AI_TAKEOVER_UNANSWERED",
                        )
                    )
                # A conversa passa a ser da IA já agora, para a distribuição não
                # devolvê-la ao vendedor antes de o worker responder.
                ai_state = db.scalar(
                    select(AIConversationState).where(
                        AIConversationState.conversation_id == conversation.id
                    )
                )
                created_state = ai_state is None
                previous_state = ai_state.state if ai_state else None
                previous_agent = ai_state.agent_id if ai_state else None
                if created_state:
                    ai_state = AIConversationState(
                        conversation_id=conversation.id,
                        agent_id=binding.agent_id,
                        state="AI_ACTIVE",
                    )
                    db.add(ai_state)
                else:
                    ai_state.state = "AI_ACTIVE"
                    ai_state.agent_id = binding.agent_id
                db.flush()
                job = enqueue_ai_job(db, conversation=conversation, message=latest, takeover=True)
                if not job:
                    if created_state:
                        db.delete(ai_state)
                    else:
                        ai_state.state = previous_state
                        ai_state.agent_id = previous_agent
                if job:
                    taken += 1
                    LOG.info(
                        "ai_takeover_unanswered conversation=%s message=%s previous_owner=%s",
                        conversation.id,
                        latest.id,
                        previous_owner,
                    )
                elif previous_owner and conversation.assigned_user_id is None:
                    # A IA não pôde assumir: devolve para quem estava.
                    conversation.assigned_user_id = previous_owner
                    conversation.status = "EM_ATENDIMENTO"
        db.commit()
    finally:
        db.close()
    return taken


# Supervisão em tempo real: nenhuma mensagem do cliente fica sem resposta.
STALLED_AFTER = timedelta(minutes=2)
STALLED_WINDOW = timedelta(hours=6)
STALLED_RETRY_MARK = "[retomado pela supervisão]"
_STALLED_JOB_STATUSES = {"FALHOU", "IGNORADO", "CANCELADO"}


def _answer_stalled_conversations(*, limit: int = 300) -> int:
    """Acha cliente sem resposta e faz a IA responder.

    Cobre o que escapava das outras rotinas: mensagem que caiu em estado de
    handoff humano (o "Bom dia" sem resposta de 06/11), job que falhou ou foi
    ignorado e mensagem com mais de 10 minutos sem job. Cada mensagem é
    retomada uma única vez, para não entrar em laço.
    """
    from app.services.ai_sdr import human_is_attending  # noqa: PLC0415

    db = SessionLocal()
    answered = 0
    try:
        now = utcnow()
        cutoff = _real_mode_cutoff()
        since = now - STALLED_WINDOW
        if cutoff and cutoff > since:
            since = cutoff
        latest_ids = list(
            db.scalars(
                select(Message.id)
                .where(
                    Message.direction == "INBOUND",
                    Message.message_type == "TEXT",
                    Message.created_at >= since,
                    Message.created_at <= now - STALLED_AFTER,
                )
                .order_by(Message.id.desc())
                .limit(limit)
            )
        )
        seen: set[int] = set()
        for message_id in latest_ids:
            message = db.get(Message, message_id)
            if not message or message.conversation_id in seen:
                continue
            seen.add(message.conversation_id)
            later = db.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == message.conversation_id,
                    Message.id > message.id,
                    Message.direction.in_(["INBOUND", "OUTBOUND"]),
                )
                .limit(1)
            )
            if later:
                continue
            conversation = db.get(Conversation, message.conversation_id)
            if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
                continue
            state = db.scalar(
                select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
            )
            if not state or state.state in {"OPT_OUT", "COMPLETED", "SHADOW_REVIEW"}:
                # Sem estado = a IA nunca atendeu: fica com a recuperação normal / fila.
                continue
            if human_is_attending(db, conversation):
                continue
            job = db.scalar(
                select(AIJob).where(AIJob.message_id == message.id).order_by(AIJob.id.desc()).limit(1)
            )
            if job and job.status in {"PENDENTE", "PROCESSANDO"}:
                continue
            if job and (job.status not in _STALLED_JOB_STATUSES or STALLED_RETRY_MARK in (job.error or "")):
                continue
            if job:
                job.status = "PENDENTE"
                job.attempts = 0
                job.locked_at = None
                job.available_at = now
                job.error = f"{STALLED_RETRY_MARK} {job.error or ''}"[:2000]
                if state.state in {"CLOSING", "AI_ACTIVE"} and state.agent_id:
                    job.agent_id = state.agent_id
                resumed = job
            else:
                resumed = enqueue_ai_job(db, conversation=conversation, message=message)
            if resumed:
                answered += 1
                LOG.warning(
                    "ai_supervision_stalled_resumed conversation=%s message=%s state=%s job=%s",
                    conversation.id,
                    message.id,
                    state.state,
                    resumed.id,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return answered


def run() -> None:
    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    poll = max(2, int(settings.ai_sdr_poll_seconds or 3))
    LOG.info(
        "SulIA worker iniciado. sdr_enabled=%s seller_enabled=%s maintenance=%s supervisor_shadow=%s guardian=%s autocorrect=%s learning=%s engineer=%s openai_configured=%s poll=%ss",
        settings.ai_sdr_enabled,
        settings.ai_seller_enabled,
        settings.ai_maintenance_enabled,
        settings.ai_supervisor_shadow_enabled,
        settings.ai_supervisor_guardian_enabled
        or settings.ai_supervisor_guardian_shadow_enabled,
        settings.ai_autocorrect_enabled,
        settings.ai_learning_enabled,
        settings.ai_engineer_enabled,
        bool(settings.openai_api_key),
        poll,
    )
    if settings.ai_maintenance_enabled and settings.ai_supervisor_shadow_enabled:
        supervisor_thread = threading.Thread(
            target=run_shadow_supervisor_loop,
            name="sulia-supervisor-shadow",
            daemon=True,
        )
        supervisor_thread.start()
    if settings.ai_maintenance_enabled and settings.ai_autocorrect_enabled:
        threading.Thread(
            target=run_autocorrector_loop,
            name="sulia-autocorrector",
            daemon=True,
        ).start()
    if settings.ai_maintenance_enabled and settings.ai_learning_enabled:
        threading.Thread(
            target=run_learning_loop,
            name="sulia-learning",
            daemon=True,
        ).start()
    if settings.ai_maintenance_enabled and settings.ai_engineer_enabled:
        threading.Thread(
            target=run_engineer_loop,
            name="sulia-engineer",
            daemon=True,
        ).start()
    if settings.ai_maintenance_enabled and settings.ai_weekly_analyst_enabled:
        threading.Thread(
            target=run_weekly_analyst_loop,
            name="sulia-weekly-analyst",
            daemon=True,
        ).start()
    activated_agents = _ensure_bound_ai_agents_active()
    if activated_agents:
        LOG.warning("Agentes IA vinculados reativados=%s", activated_agents)
    cancelled_jobs, cancelled_followups = _cancel_pre_real_mode_work()
    if cancelled_jobs or cancelled_followups:
        LOG.warning("Modo real ativado: jobs antigos cancelados=%s followups antigos cancelados=%s cutoff=%s", cancelled_jobs, cancelled_followups, _real_mode_cutoff())
    _recover_stale_jobs()
    _recover_stale_followups()
    recovered_closer = _recover_closer_capture_patch_failures()
    if recovered_closer:
        LOG.info(
            "Fechamentos do Vendedor IA recuperados apos correcao de PATCH CRM=%s",
            recovered_closer,
        )

    resumed_mobile = 0
    db = SessionLocal()
    try:
        resumed_mobile = resume_simplified_mobile_states(db, since=_real_mode_cutoff())
        db.commit()
    finally:
        db.close()
    if resumed_mobile:
        LOG.info(
            "Fechamentos moveis reposicionados apos simplificacao do fluxo=%s",
            resumed_mobile,
        )
    recovered_recent = _recover_recent_unqueued_inbounds()
    if recovered_recent:
        LOG.info("Mensagens recentes sem job recuperadas=%s", recovered_recent)
    backfilled_followups = _backfill_missing_followups()
    if backfilled_followups:
        LOG.info("Follow-ups ausentes reconstruidos=%s", backfilled_followups)

    missing_key_logged = False
    last_inbound_recovery = time.monotonic()
    last_stale_recovery = time.monotonic()
    while True:
        if not settings.ai_sdr_enabled and not settings.ai_seller_enabled:
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

        now_mono = time.monotonic()
        if now_mono - last_inbound_recovery >= 60:
            try:
                recovered_recent = _recover_recent_unqueued_inbounds()
                if recovered_recent:
                    LOG.info("Mensagens recentes sem job recuperadas=%s", recovered_recent)
            except Exception:
                LOG.exception("Falha na recuperação periódica de mensagens sem job.")
            try:
                _takeover_unanswered_human_conversations()
            except Exception:
                LOG.exception("Falha ao assumir conversas sem resposta do vendedor.")
            try:
                stalled = _answer_stalled_conversations()
                if stalled:
                    LOG.warning("Supervisão: conversas sem resposta retomadas=%s", stalled)
            except Exception:
                LOG.exception("Falha na supervisão de conversas sem resposta.")
            last_inbound_recovery = now_mono

        if now_mono - last_stale_recovery >= 300:
            try:
                old_jobs, old_followups = _cancel_pre_real_mode_work()
                stale_jobs = _recover_stale_jobs()
                stale_followups = _recover_stale_followups()
                backfilled = _backfill_missing_followups(limit=250)
                if old_jobs or old_followups or stale_jobs or stale_followups or backfilled:
                    LOG.info(
                        "Recuperação periódica old_jobs=%s old_followups=%s stale jobs=%s followups=%s backfilled=%s",
                        old_jobs,
                        old_followups,
                        stale_jobs,
                        stale_followups,
                        backfilled,
                    )
            except Exception:
                LOG.exception("Falha na recuperação periódica de jobs.")
            last_stale_recovery = now_mono

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

            # Follow-up tem cota própria em toda volta do worker. Assim ele não
            # fica eternamente atrás de novas mensagens inbound.
            followup = claim_followup_job(db)
            if followup:
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
            elif not job:
                contract_updates = poll_pending_contracts(db)
                db.commit()
                if contract_updates:
                    LOG.info("ai_closer_contract_updates=%s", contract_updates)
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
