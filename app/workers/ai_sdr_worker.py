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
    if not cutoff or not message.created_at or message.created_at < cutoff:
        return False

    state = db.scalar(select(AIConversationState).where(
        AIConversationState.conversation_id == conversation.id
    ))
    closing = db.scalar(select(AIClosingState).where(
        AIClosingState.conversation_id == conversation.id
    ))

    legacy_state = bool(
        (state and state.created_at and state.created_at < cutoff)
        or (closing and closing.created_at and closing.created_at < cutoff)
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
                if not closing or closing.step not in SELLER_CUSTOMER_INPUT_STEPS:
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
            if cutoff and latest.created_at and latest.created_at < cutoff:
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
        threshold = utcnow() - timedelta(minutes=30)
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
    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    poll = max(2, int(settings.ai_sdr_poll_seconds or 3))
    LOG.info(
        "SulIA worker iniciado. sdr_enabled=%s seller_enabled=%s maintenance=%s supervisor_shadow=%s guardian=%s autocorrect=%s learning=%s engineer=%s openai_configured=%s poll=%ss",
        settings.ai_sdr_enabled,
        settings.ai_seller_enabled,
        settings.ai_maintenance_enabled,
        settings.ai_supervisor_shadow_enabled,
        settings.ai_supervisor_guardian_enabled,
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
