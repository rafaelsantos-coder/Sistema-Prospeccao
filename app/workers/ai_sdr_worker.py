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
from app.models import AIAgentProfile, AIFollowupJob, AIClosingState, AIConversationState, AIJob, AIQueueBinding, AIRun, AuditLog, Conversation, ConversationQueueLink, Message
from app.services.ai_followups import claim_followup_job, process_followup_job, schedule_followup
from app.services.ai_pending_scope import UNREAD_RECOVERY_SINCE_UTC, in_unread_recovery_window
from app.services.ai_queue_scope import ai_allowed_for_conversation, ai_allowed_for_queue, ai_queue_enabled, is_dedicated_ai_queue, manual_queue_control_enabled, enforce_exclusive_ai_queue, enforce_general_commercial_human_only, exclusive_ai_queue_id
from app.services.ai_queue_coverage import (
    COVERAGE_ACTION,
    COVERAGE_CAMPAIGN_KEY,
    ensure_unanswered_coverage_all_queues,
)
from app.services.ai_sdr import AI_INBOUND_MESSAGE_TYPES, claim_ai_job, enqueue_ai_job, process_ai_job, queue_ai_binding
from app.services.ai_seller import (
    active_closer,
    poll_pending_contracts,
    resume_simplified_mobile_states,
)
from app.services.ai_supervisor import run_shadow_supervisor_loop
from app.services.commercial_intelligence import run_commercial_observer_loop
from app.services.ai_autocorrector import run_autocorrector_loop
from app.services.ai_learning import run_learning_loop
from app.services.ai_engineer import run_engineer_loop
from app.services.ai_weekly_analyst import run_weekly_analyst_loop
from app.services.ai_sdr_policy import followup_spec
from app.services.queue_routing import MANUAL_AI_ORIGIN, MANUAL_TRANSFER_ORIGIN, bind_conversation_queue, queue_for_conversation
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
        stmt = select(AIQueueBinding).where(
            AIQueueBinding.active.is_(True),
            AIQueueBinding.handling_mode != "HUMAN",
        )
        if manual_queue_control_enabled():
            stmt = stmt.where(AIQueueBinding.manual_authorized.is_(True))
        bindings = list(db.scalars(stmt))
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
            from app.services.ai_followups import followup_delay_for, hot_lead_awaits_customer
            if hot_lead_awaits_customer(db, job.conversation_id):
                # Preserva a pendência original em vez de substituir por tema genérico.
                delay = followup_delay_for(db, job.conversation_id, int(job.step or 1))
            else:
                delay, theme = followup_spec(int(job.step or 1))
                job.theme = theme
            job.due_at = now + delay
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
            if not ai_allowed_for_conversation(db, conversation.id):
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
                or (conversation.lead and conversation.lead.do_not_contact)
                or (conversation.lead and conversation.lead.stage in {"GANHO", "NAO_CONTATAR"})
            ):
                continue

            # Uma conversa já encerrada, sem permissão ou com cliente que pediu
            # para não ser contatado não pode ser reativada pelo backfill.
            from app.services.ai_followups import _customer_declined, _queue_ai_paused
            if _customer_declined(db, conversation.id) or _queue_ai_paused(db, conversation.id):
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
            # Não recriar tarefas canceladas por limite/recusa/guardião.
            # Em 16/11 o mesmo cliente recebia 25 tarefas distintas em 2 horas,
            # todas canceladas pelo processamento seguinte.
            from app.services.ai_followups import (
                MAX_UNANSWERED_CLOSING_FOLLOWUPS,
                MAX_UNANSWERED_HOT_FOLLOWUPS,
                MAX_UNANSWERED_FOLLOWUPS,
                closing_awaits_customer,
                hot_lead_awaits_customer,
                followup_delay_for,
                _followup_allowed,
            )
            limit_unanswered = (
                MAX_UNANSWERED_CLOSING_FOLLOWUPS
                if closing_awaits_customer(db, conversation.id)
                else MAX_UNANSWERED_HOT_FOLLOWUPS
                if hot_lead_awaits_customer(db, conversation.id)
                else MAX_UNANSWERED_FOLLOWUPS
            )
            sent_since_reply = select(AIFollowupJob.id).where(
                AIFollowupJob.conversation_id == conversation.id,
                AIFollowupJob.status == "ENVIADO",
                AIFollowupJob.sent_at.is_not(None),
            )
            last_inbound = db.scalar(
                select(Message.created_at)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == "INBOUND",
                )
                .order_by(Message.id.desc()).limit(1)
            )
            if last_inbound:
                sent_since_reply = sent_since_reply.where(
                    AIFollowupJob.sent_at > utc_naive(last_inbound)
                )
            sent_count = len(list(db.scalars(sent_since_reply)))
            if sent_count >= limit_unanswered:
                continue
            last_stopped = db.scalar(
                select(AIFollowupJob.id).where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.source_message_id == source_inbound_id,
                    AIFollowupJob.status == "CANCELADO",
                    AIFollowupJob.error.in_(
                        ["CLIENTE_DESISTIU", "LIMITE_SEM_RESPOSTA", "GUARDIAN_OPT_OUT"]
                    ),
                ).limit(1)
            )
            if last_stopped:
                continue

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
                delay = followup_delay_for(db, conversation.id, next_step)
                last_outbound_at = utc_naive(latest.created_at) or utcnow()
                ideal_due = last_outbound_at + delay
                # Sem janela comercial: atrasados ficam elegíveis imediatamente,
                # sem estender a espera por mais um ciclo inteiro.
                now_utc = utcnow()
                job.due_at = max(now_utc, ideal_due)
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
        threshold = max(utcnow() - timedelta(minutes=10), UNREAD_RECOVERY_SINCE_UTC)
        if cutoff and cutoff > threshold:
            threshold = cutoff
        messages = list(
            db.scalars(
                select(Message)
                .where(
                    Message.direction == "INBOUND",
                    Message.message_type.in_(AI_INBOUND_MESSAGE_TYPES),
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

            if not ai_allowed_for_conversation(db, conversation.id):
                reasons["exclusive_ai_queue"] += 1
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

            manual_transfer = bool(
                current_queue
                and db.scalar(
                    select(ConversationQueueLink.id).where(
                        ConversationQueueLink.conversation_id == conversation.id,
                        ConversationQueueLink.origin.in_((MANUAL_TRANSFER_ORIGIN, MANUAL_AI_ORIGIN)),
                    )
                )
            )
            if manual_transfer:
                # Alguém transferiu a conversa de fila na tela: a recuperação não
                # devolve para a fila do número.
                reasons["manual_transfer_kept"] += 1
            elif instance and target_queue and target_binding:
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
UNANSWERED_TAKEOVER_WINDOW = timedelta(hours=24)
_TAKEOVER_BLOCKED_STATES = {
    "AI_ACTIVE",
    "CLOSING",
    "OPT_OUT",
    "COMPLETED",
    "SHADOW_REVIEW",
}


def _takeover_unanswered_human_conversations(*, limit: int = 50) -> int:
    from app.models import ConversationAssignment, ConversationQueueLink
    from app.services.queue_routing import AI_WHEN_OFFLINE_MODES, human_online_in_queue
    from app.services.audit import write_audit

    db = SessionLocal()
    taken = 0
    try:
        now = utcnow()
        cutoff = max(_real_mode_cutoff() or UNREAD_RECOVERY_SINCE_UTC, UNREAD_RECOVERY_SINCE_UTC)
        all_queues_recovery = bool(db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == COVERAGE_ACTION,
                AuditLog.entity_id == COVERAGE_CAMPAIGN_KEY,
            ).limit(1)
        ))
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
            if not ai_queue_enabled(db, binding.queue_id):
                continue
            enabled_at = utc_naive(binding.updated_at)
            human_online = human_online_in_queue(db, binding.queue_id)
            threshold = UNANSWERED_TAKEOVER_AFTER if human_online else timedelta(minutes=2)
            conversations = list(
                db.scalars(
                    select(Conversation)
                    .join(ConversationQueueLink, ConversationQueueLink.conversation_id == Conversation.id)
                    .where(
                        ConversationQueueLink.queue_id == binding.queue_id,
                        Conversation.status.not_in(["RESOLVIDO", "ENCERRADO", "BLOQUEADO"]),
                        Conversation.updated_at >= max(now - UNANSWERED_TAKEOVER_WINDOW, cutoff),
                    )
                    .order_by(Conversation.updated_at.desc())
                    .limit(limit)
                )
            )
            for conversation in conversations:
                # Um histórico pode ter múltiplas transferências de fila.
                # A IA atua apenas no vínculo atual, não em filas anteriores.
                current_queue = queue_for_conversation(db, conversation.id)
                if not current_queue or current_queue.id != binding.queue_id:
                    continue
                if conversation.lead and (
                    conversation.lead.do_not_contact
                    or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
                ):
                    continue
                state = db.scalar(
                    select(AIConversationState.state).where(
                        AIConversationState.conversation_id == conversation.id
                    )
                )
                if state in _TAKEOVER_BLOCKED_STATES:
                    continue
                # Fechamentos já iniciados continuam com o Vendedor IA;
                # falhas comerciais são notificadas à gestão, nunca reiniciadas no SDR.
                closing = db.scalar(
                    select(AIClosingState.id).where(
                        AIClosingState.conversation_id == conversation.id,
                        AIClosingState.completed_at.is_(None),
                    ).limit(1)
                )
                if closing:
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
                if not latest or latest.direction != "INBOUND" or latest.message_type not in AI_INBOUND_MESSAGE_TYPES:
                    continue
                received_at = utc_naive(latest.created_at)
                if not received_at or now - received_at < threshold:
                    continue
                if now - received_at > UNANSWERED_TAKEOVER_WINDOW:
                    continue
                if not in_unread_recovery_window(received_at, now=now) or received_at < cutoff:
                    continue
                # Mutirão autorizado: somente mensagens desde 18h de 16/11/2026,
                # mesmo que a IA tenha sido vinculada à fila depois disso.
                if enabled_at and received_at < enabled_at and not all_queues_recovery:
                    continue
                # Job ignorado (ex.: "humano atendendo") não conta como resposta: se a
                # pessoa não respondeu em 10 min, a IA assume (conversa Claudia, 10/11:
                # cliente ficou 2h19 sem resposta).
                existing_job = db.scalar(
                    select(AIJob).where(AIJob.message_id == latest.id).limit(1)
                )
                if existing_job and existing_job.status in {"PENDENTE", "PROCESSANDO", "CONCLUIDO"}:
                    continue
                if existing_job and "AI_OFFLINE_RECOVERY" in (existing_job.error or ""):
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
                if existing_job:
                    # O enfileirador retorna jobs IGNORADOS já existentes sem
                    # reativá-los. Recuperar o registro único explicitamente.
                    existing_job.status = "PENDENTE"
                    existing_job.agent_id = binding.agent_id
                    existing_job.queue_id = binding.queue_id
                    existing_job.available_at = now
                    existing_job.locked_at = None
                    existing_job.attempts = 0
                    existing_job.error = "AI_OFFLINE_RECOVERY"
                    job = existing_job
                else:
                    job = enqueue_ai_job(db, conversation=conversation, message=latest, takeover=True)
                if not job:
                    if created_state:
                        db.delete(ai_state)
                    else:
                        ai_state.state = previous_state
                        ai_state.agent_id = previous_agent
                if job:
                    taken += 1
                    write_audit(
                        db,
                        user=None,
                        action="AI_UNREAD_TAKEOVER",
                        entity_type="conversation",
                        entity_id=conversation.id,
                        payload={
                            "queue_id": binding.queue_id,
                            "message_id": latest.id,
                            "previous_owner": previous_owner,
                            "human_online": human_online,
                        },
                    )
                    LOG.info(
                        "ai_takeover_unanswered conversation=%s message=%s queue=%s human_online=%s",
                        conversation.id,
                        latest.id,
                        binding.queue_id,
                        human_online,
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
    from app.services.ai_sdr import human_is_attending, human_stepped_in  # noqa: PLC0415

    db = SessionLocal()
    answered = 0
    try:
        now = utcnow()
        cutoff = _real_mode_cutoff()
        since = max(now - STALLED_WINDOW, UNREAD_RECOVERY_SINCE_UTC)
        if cutoff and cutoff > since:
            since = cutoff
        latest_ids = list(
            db.scalars(
                select(Message.id)
                .where(
                    Message.direction == "INBOUND",
                    Message.message_type.in_(AI_INBOUND_MESSAGE_TYPES),
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
            if not ai_allowed_for_conversation(db, conversation.id):
                continue
            state = db.scalar(
                select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
            )
            if not state or state.state in {"OPT_OUT", "COMPLETED", "SHADOW_REVIEW"}:
                # Sem estado = a IA nunca atendeu: fica com a recuperação normal / fila.
                continue
            current_queue = queue_for_conversation(db, conversation.id)
            if human_is_attending(db, conversation) or (
                not is_dedicated_ai_queue(current_queue) and human_stepped_in(db, conversation)
            ):
                # Não sobrescrever o responsável humano. Os alertas operacionais
                # continuam cobrando resposta, mesmo sem a IA intervir.
                continue
            # Em fila de IA quando humano está online, "online" sozinho não
            # significa que alguém respondeu. Depois do prazo de retomada,
            # ignorar a presença online e enfileirar a IA.
            if state.state == "AI_ACTIVE":
                from app.services.ai_sdr import binding_for_conversation
                from app.services.queue_routing import AI_WHEN_HUMAN_IDLE
                binding = binding_for_conversation(db, conversation.id)
                if binding and binding.handling_mode == AI_WHEN_HUMAN_IDLE:
                    age = now - (utc_naive(message.created_at) or now)
                    if age < UNANSWERED_TAKEOVER_AFTER:
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
                resumed = enqueue_ai_job(
                    db,
                    conversation=conversation,
                    message=message,
                    takeover=True,
                )
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



def _review_yesterday_and_today_fila_ia() -> dict[str, int]:
    """Audita mensagens de ontem e hoje da FILA-IA, recuperando pendências elegíveis.

    Nunca transfere filas, reabre conversa encerrada nem reenvia quando existe
    resposta posterior, job de IA prévio ou responsável humano.
    """
    from zoneinfo import ZoneInfo

    from app.models import ServiceQueue
    from app.services.ai_sdr import human_is_attending, human_stepped_in
    from app.services.queue_routing import queue_for_conversation

    counts: Counter[str] = Counter()
    with SessionLocal() as db:
        queue_candidates = list(db.scalars(select(ServiceQueue).where(
            ServiceQueue.active.is_(True),
        ).order_by(ServiceQueue.id.asc())))
        dedicated_queues = [queue for queue in queue_candidates if is_dedicated_ai_queue(queue)]
        queue = next((item for item in dedicated_queues if ai_queue_enabled(db, item.id)), None)
        if queue is None:
            LOG.error(
                "ai_today_review_not_authorized: dedicated_queue_ids=%s active_binding_missing",
                [item.id for item in dedicated_queues],
            )
            return {"not_authorized": 1}

        local_now = datetime.now(ZoneInfo(settings.default_timezone))
        since_local = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_utc = since_local.astimezone(timezone.utc).replace(tzinfo=None)
        # Paginar sem limite global para cobrir todas as mensagens de ontem e hoje.
        last_id = 0
        seen: set[int] = set()
        while True:
            batch = list(db.scalars(
                select(Message)
                .join(ConversationQueueLink, ConversationQueueLink.conversation_id == Message.conversation_id)
                .where(
                    ConversationQueueLink.queue_id == queue.id,
                    Message.direction == "INBOUND",
                    Message.created_at >= since_utc,
                    Message.id > last_id,
                )
                .order_by(Message.id.asc())
                .limit(500)
            ))
            if not batch:
                break
            last_id = batch[-1].id
            for message in batch:
                counts["received_yesterday_today"] += 1
                if message.conversation_id in seen:
                    continue
                seen.add(message.conversation_id)
                counts["conversations"] += 1
                conversation = db.get(Conversation, message.conversation_id)
                if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
                    counts["closed"] += 1
                    continue
                current_queue = queue_for_conversation(db, conversation.id)
                if not current_queue or current_queue.id != queue.id:
                    counts["moved_queue"] += 1
                    continue
                if conversation.lead and (
                    conversation.lead.do_not_contact
                    or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
                ):
                    counts["do_not_contact"] += 1
                    continue
                # Fila IA é de atendimento automatizado. Mensagem manual
                # anterior não bloqueia resposta ao novo INBOUND, mas um humano
                # com conversa realmente atribuída e ativa continua protegido.
                if human_is_attending(db, conversation):
                    counts["human_owned"] += 1
                    continue
                state = db.scalar(select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id,
                ))
                if state and state.state in {"OPT_OUT", "COMPLETED"}:
                    counts["terminal_ai_state"] += 1
                    continue

                latest = db.scalar(select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.direction.in_(["INBOUND", "OUTBOUND"]),
                ).order_by(Message.id.desc()).limit(1))
                if not latest or latest.direction != "INBOUND":
                    counts["answered"] += 1
                    continue
                if latest.message_type not in AI_INBOUND_MESSAGE_TYPES:
                    counts["unsupported_type"] += 1
                    continue
                if state and state.state == "SHADOW_REVIEW":
                    # A revisão em SHADOW não enviou mensagem. Novo inbound sem
                    # resposta humana precisa poder voltar ao fluxo SDR real.
                    state.state = "NEW"
                    counts["shadow_state_resumed"] += 1
                prior_job = db.scalar(select(AIJob).where(
                    AIJob.message_id == latest.id,
                ).limit(1))
                if prior_job:
                    # Jobs concluídos em SHADOW não responderam ao cliente.
                    # Retomar uma única vez, somente quando a última mensagem
                    # continuar sendo INBOUND e a fila permanecer FILA-IA.
                    shadow_run = db.scalar(
                        select(AIRun)
                        .where(
                            AIRun.job_id == prior_job.id,
                            AIRun.execution_mode == "SHADOW",
                            AIRun.status == "CONCLUIDO",
                        )
                        .order_by(AIRun.id.desc())
                        .limit(1)
                    )
                    if prior_job.status == "CONCLUIDO" and shadow_run:
                        prior_job.status = "PENDENTE"
                        prior_job.available_at = utcnow()
                        prior_job.locked_at = None
                        prior_job.attempts = 0
                        prior_job.error = None
                        db.commit()
                        counts["requeued_shadow"] += 1
                        continue
                    if (
                        prior_job.status == "FALHOU"
                        and "SDR perdeu a propriedade da conversa" in (prior_job.error or "")
                        and (not state or state.state not in {"OPT_OUT", "COMPLETED", "CLOSING"})
                    ):
                        # Reconciliar a tarefa com a fila e o SDR atuais ANTES
                        # de recolocar na fila. Sem isso a tentativa falha em
                        # loop se o job veio de uma fila antiga.
                        binding = queue_ai_binding(db, queue.id)
                        if not (binding and binding.agent and binding.agent.active):
                            counts["owner_failure_binding_missing"] += 1
                            continue
                        prior_job.queue_id = queue.id
                        prior_job.agent_id = binding.agent_id
                        prior_job.status = "PENDENTE"
                        prior_job.available_at = utcnow()
                        prior_job.locked_at = None
                        prior_job.attempts = 0
                        prior_job.error = None
                        db.commit()
                        counts["requeued_owner_failure"] += 1
                        continue
                    # A fila antes nao possuia autorizacao no banco: jobs
                    # ignorados/fracassados por esse motivo ficaram sem resposta.
                    # Recuperar uma unica vez somente quando o ultimo evento
                    # continua sendo do cliente e a fila agora esta autorizada.
                    previous_error = str(prior_job.error or "").casefold()
                    queue_failure = any(reason in previous_error for reason in (
                        "pausada na fila", "fila sem ia", "sem vínculo",
                        "sem vinculo", "no_binding", "propriedade da conversa",
                        "fila exclusiva de atendimento humano",
                        "ia autorizada somente na fila ia",
                        "agente inexistente ou inativo",
                    ))
                    if (
                        prior_job.status in {"FALHOU", "IGNORADO", "CANCELADO"}
                        and queue_failure
                        and "REQUEUED_DEDICATED_AI_QUEUE_20261121" not in (prior_job.error or "")
                        and not (state and state.state in {"OPT_OUT", "COMPLETED"})
                    ):
                        binding = queue_ai_binding(db, queue.id)
                        if binding and binding.agent and binding.agent.active:
                            prior_job.queue_id = queue.id
                            if not (state and state.state == "CLOSING"):
                                prior_job.agent_id = binding.agent_id
                            prior_job.status = "PENDENTE"
                            prior_job.available_at = utcnow()
                            prior_job.locked_at = None
                            prior_job.attempts = 0
                            prior_job.error = "REQUEUED_DEDICATED_AI_QUEUE_20261121"
                            db.commit()
                            counts["requeued_binding_error"] += 1
                            continue
                    counts["already_job_" + str(prior_job.status).lower()] += 1
                    continue
                try:
                    job = enqueue_ai_job(db, conversation=conversation, message=latest)
                    if job:
                        counts["queued_for_reply"] += 1
                    else:
                        counts["not_eligible"] += 1
                    db.commit()
                except Exception:
                    db.rollback()
                    counts["errors"] += 1
                    LOG.exception("ai_today_review_enqueue_failed conversation=%s", conversation.id)
        LOG.warning("ai_yesterday_today_fila_ia_review %s", dict(counts))
    return dict(counts)


def run() -> None:
    from app.services.ai_reply_delivery import process_next_ai_reply
    from app.services.crm_notes import check_completed_activation

    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    # Executar a exclusividade ANTES de qualquer recuperacao ou emissao da IA.
    if exclusive_ai_queue_id() is not None:
        with SessionLocal() as scope_db:
            scope_result = enforce_exclusive_ai_queue(scope_db)
            scope_db.commit()
            LOG.warning("Escopo IA exclusivo preparado: %s", scope_result)
    # Mesmo com controle manual de filas, a Geral Comercial nunca recebe IA.
    with SessionLocal() as human_scope_db:
        general_result = enforce_general_commercial_human_only(human_scope_db)
        human_scope_db.commit()
        LOG.warning("Geral Comercial em modo humano: %s", general_result)
    # Observador comercial isolado: roda sem permissão de enviar WhatsApp,
    # independentemente do escopo de atuação do SDR/Vendedor IA.
    threading.Thread(
        target=run_commercial_observer_loop,
        name="sulnet-commercial-observer",
        daemon=True,
    ).start()
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
    with SessionLocal() as readiness_db:
        LOG.info(
            "sulia_reply_delivery_ready closer_ready=%s",
            active_closer(readiness_db) is not None,
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
    if settings.ai_sdr_enabled and settings.ai_autonomous_enabled:
        try:
            with SessionLocal() as coverage_db:
                coverage_result = ensure_unanswered_coverage_all_queues(coverage_db)
                if coverage_result.get("error"):
                    LOG.error("Cobertura de IA indisponível: %s", coverage_result["error"])
        except Exception:
            LOG.exception("Falha ao preparar cobertura IA de todas as filas.")
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
    _review_yesterday_and_today_fila_ia()
    if settings.ai_sdr_enabled and settings.ai_autonomous_enabled:
        try:
            claimed = _takeover_unanswered_human_conversations()
            LOG.warning("Pendências assumidas na inicialização=%s", claimed)
        except Exception:
            LOG.exception("Falha na recuperação inicial de mensagens não lidas.")
    backfilled_followups = _backfill_missing_followups()
    if backfilled_followups:
        LOG.info("Follow-ups ausentes reconstruidos=%s", backfilled_followups)

    missing_key_logged = False
    last_inbound_recovery = time.monotonic()
    last_fila_ia_review = time.monotonic()
    last_ops_alerts = time.monotonic() - 90
    last_crm_prospect = time.monotonic() - 100
    last_activation_check = time.monotonic()
    last_stale_recovery = time.monotonic()
    last_scope_enforcement = time.monotonic()
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
        if now_mono - last_scope_enforcement >= 60:
            try:
                with SessionLocal() as scope_db:
                    if exclusive_ai_queue_id() is not None:
                        enforce_exclusive_ai_queue(scope_db)
                    enforce_general_commercial_human_only(scope_db)
                    scope_db.commit()
            except Exception:
                LOG.exception("Falha na reconciliacao das filas IA e Geral Comercial")
            last_scope_enforcement = now_mono
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

        if now_mono - last_fila_ia_review >= 120:
            try:
                _review_yesterday_and_today_fila_ia()
            except Exception:
                LOG.exception("Falha na revisão periódica da Fila IA")
            last_fila_ia_review = now_mono

        if now_mono - last_ops_alerts >= 120:
            last_ops_alerts = now_mono
            try:
                from app.services.ops_alerts import run_ops_alert_checks

                with SessionLocal() as alert_db:
                    run_ops_alert_checks(alert_db)
            except Exception:
                LOG.exception("Falha nos avisos operacionais.")
        if now_mono - last_crm_prospect >= 120:
            last_crm_prospect = now_mono
            try:
                from app.services.crm_prospect_sync import sync_prospects

                with SessionLocal() as prospect_db:
                    sync_prospects(prospect_db)
            except Exception:
                LOG.exception("Falha ao registrar prospecções no CRM.")

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
            process_next_ai_reply(db)
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
                    process_next_ai_reply(db)
                    # Registrar CRM logo no primeiro atendimento real da SulIA.
                    # A integração não bloqueia a resposta ao cliente; se falhar,
                    # a varredura periódica tenta recuperar sem duplicar.
                    try:
                        from app.services.crm_prospect_sync import sync_prospects

                        with SessionLocal() as prospect_db:
                            sync_prospects(
                                prospect_db, conversation_id=job.conversation_id, limit=1,
                            )
                    except Exception:
                        LOG.exception(
                            "Falha na sincronização imediata do CRM conversation=%s",
                            job.conversation_id,
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
                if settings.ai_seller_enabled and now_mono - last_activation_check >= 60:
                    check_completed_activation(db)
                    last_activation_check = now_mono
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
