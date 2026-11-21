"""Politicas de autorizacao da IA por fila.

AI_QUEUE_MANUAL_CONTROL=true: todas as filas ficam configuraveis, porem
somente vinculos ativos com aprovacao explicita do gestor podem atender.
As demais permanecem humanas. A flag legado AI_EXCLUSIVE_QUEUE_ID so vale
quando o modo manual estiver desligado.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIQueueBinding,
    Conversation,
    ConversationQueueLink,
    ServiceQueue,
)
from app.services.audit import write_audit

LOG = logging.getLogger("sulnet_ai_queue_scope")


def is_dedicated_ai_queue(queue: ServiceQueue | None) -> bool:
    """Reconhece apenas a fila dedicada de IA, inclusive seu nome na interface.

    Não confunde "Geral Comercial" com "Fila IA". Esta identificação não
    concede autorização: o vínculo ativo/permitido continua obrigatório.
    """
    if not queue or not queue.active:
        return False

    def _normalized(value: str | None) -> str:
        return " ".join(str(value or "").replace("-", " ").replace("_", " ").casefold().split())

    return _normalized(queue.code) == "fila ia" or _normalized(queue.name) == "fila ia"


def manual_queue_control_enabled() -> bool:
    return os.getenv("AI_QUEUE_MANUAL_CONTROL", "false").strip().lower() in {"1", "true", "yes", "on"}


def exclusive_ai_queue_id() -> int | None:
    if manual_queue_control_enabled():
        return None
    raw = os.getenv("AI_EXCLUSIVE_QUEUE_ID")
    if raw is None:
        return None
    try:
        parsed = int(raw.strip())
    except ValueError:
        parsed = 0
    return parsed if parsed > 0 else 0


def ai_allowed_for_queue(queue_id: int | None) -> bool:
    """Fila pode ser configurada; envio real tambem requer ai_queue_enabled."""
    if manual_queue_control_enabled():
        return queue_id is not None and int(queue_id) > 0
    exclusive = exclusive_ai_queue_id()
    return exclusive is None or bool(queue_id is not None and int(queue_id) == exclusive)


def ai_queue_enabled(db: Session, queue_id: int | None) -> bool:
    """Autorizacao efetiva: no modo manual falha fechado para vinculos legados."""
    if not ai_allowed_for_queue(queue_id):
        return False
    if not manual_queue_control_enabled():
        return True
    binding = db.scalar(
        select(AIQueueBinding)
        .join(ServiceQueue, ServiceQueue.id == AIQueueBinding.queue_id)
        .where(
            AIQueueBinding.queue_id == queue_id,
            AIQueueBinding.active.is_(True),
            AIQueueBinding.manual_authorized.is_(True),
            AIQueueBinding.handling_mode != "HUMAN",
            ServiceQueue.active.is_(True),
        )
    )
    if not binding:
        return False
    agent = db.get(AIAgentProfile, binding.agent_id)
    return bool(agent and agent.active and agent.role_type == "SDR")


def ai_allowed_for_conversation(
    db: Session,
    conversation_id: int,
    *,
    fallback_queue_id: int | None = None,
) -> bool:
    if not manual_queue_control_enabled() and exclusive_ai_queue_id() is None:
        return True
    current = db.scalar(
        select(ConversationQueueLink.queue_id).where(
            ConversationQueueLink.conversation_id == conversation_id
        )
    )
    queue_id = current if current is not None else fallback_queue_id
    return ai_queue_enabled(db, queue_id) if manual_queue_control_enabled() else ai_allowed_for_queue(queue_id)


def enforce_exclusive_ai_queue(db: Session) -> dict[str, object]:
    """Reconciliar vínculos e pendências sem excluir histórico de mensagens.

    A fila permitida continua com SDR ativo. Vínculos IA de outras filas são
    removidos, jobs pendentes cancelados e conversas de posse da IA são
    devolvidas à fila humana. Operação idempotente e auditável.
    """
    if manual_queue_control_enabled():
        return {"enabled": False, "mode": "manual_queue_control"}
    target_id = exclusive_ai_queue_id()
    if target_id is None:
        return {"enabled": False}
    target = db.get(ServiceQueue, target_id) if target_id > 0 else None

    removed_ids: list[int] = []
    target_changed = False
    for binding in list(db.scalars(select(AIQueueBinding))):
        if binding.queue_id != target_id:
            removed_ids.append(binding.queue_id)
            db.delete(binding)

    target_ready = bool(target and target.active)
    if target_ready:
        existing = db.scalar(
            select(AIQueueBinding).where(AIQueueBinding.queue_id == target_id)
        )
        selected_agent = None
        if existing:
            candidate = db.get(AIAgentProfile, existing.agent_id)
            if candidate and candidate.active and candidate.role_type == "SDR" and candidate.execution_mode == "AUTO":
                selected_agent = candidate
        if selected_agent is None:
            selected_agent = db.scalar(
                select(AIAgentProfile)
                .where(
                    AIAgentProfile.slug == "sulia-sdr-digital",
                    AIAgentProfile.role_type == "SDR",
                    AIAgentProfile.active.is_(True),
                    AIAgentProfile.execution_mode == "AUTO",
                )
                .limit(1)
            )
        if selected_agent is None:
            selected_agent = db.scalar(
                select(AIAgentProfile)
                .where(
                    AIAgentProfile.role_type == "SDR",
                    AIAgentProfile.active.is_(True),
                    AIAgentProfile.execution_mode == "AUTO",
                )
                .order_by(AIAgentProfile.id)
                .limit(1)
            )
        if selected_agent:
            if existing is None:
                existing = AIQueueBinding(
                    queue_id=target_id,
                    agent_id=selected_agent.id,
                    handling_mode="AI_FIRST",
                    active=True,
                    fallback_to_human=True,
                )
                db.add(existing)
                target_changed = True
            else:
                if existing.agent_id != selected_agent.id or not existing.active or existing.handling_mode == "HUMAN":
                    target_changed = True
                existing.agent_id = selected_agent.id
                existing.active = True
                if existing.handling_mode == "HUMAN":
                    existing.handling_mode = "AI_FIRST"
        elif existing:
            # Sem agente válido, falhar fechado em vez de usar IA inativa.
            if existing.active:
                target_changed = True
            existing.active = False
            target_ready = False
        else:
            target_ready = False

    not_target = or_(
        ConversationQueueLink.queue_id != target_id,
        ConversationQueueLink.queue_id.is_(None),
    )
    cancelled_jobs = 0
    jobs = list(
        db.scalars(
            select(AIJob)
            .outerjoin(
                ConversationQueueLink,
                ConversationQueueLink.conversation_id == AIJob.conversation_id,
            )
            .where(
                AIJob.status.in_(("PENDENTE", "PROCESSANDO")),
                not_target,
            )
        )
    )
    for job in jobs:
        job.status = "CANCELADO"
        job.locked_at = None
        job.error = "Atendimento IA permitido somente na Fila IA."
        cancelled_jobs += 1

    cancelled_followups = 0
    followups = list(
        db.scalars(
            select(AIFollowupJob)
            .outerjoin(
                ConversationQueueLink,
                ConversationQueueLink.conversation_id == AIFollowupJob.conversation_id,
            )
            .where(
                AIFollowupJob.status.in_(("PENDENTE", "PROCESSANDO")),
                not_target,
            )
        )
    )
    for followup in followups:
        followup.status = "CANCELADO"
        followup.locked_at = None
        followup.error = "Atendimento IA permitido somente na Fila IA."
        cancelled_followups += 1

    ai_user_ids = set(db.scalars(select(AIAgentProfile.user_id)))
    released_conversations = 0
    states = list(
        db.scalars(
            select(AIConversationState)
            .join(
                ConversationQueueLink,
                ConversationQueueLink.conversation_id == AIConversationState.conversation_id,
                isouter=True,
            )
            .where(
                AIConversationState.state.in_(("AI_ACTIVE", "CLOSING")),
                not_target,
            )
        )
    )
    for state in states:
        state.state = "HUMAN_ACTIVE"
        state.handoff_at = datetime.now(timezone.utc).replace(tzinfo=None)
        conversation = db.get(Conversation, state.conversation_id)
        if conversation and (conversation.assigned_user_id is None or conversation.assigned_user_id in ai_user_ids):
            conversation.assigned_user_id = None
            if conversation.status not in ("RESOLVIDO", "ENCERRADO", "BLOQUEADO"):
                conversation.status = "EM_FILA"
        released_conversations += 1

    changed = bool(
        removed_ids or cancelled_jobs or cancelled_followups or released_conversations
        or target_changed
    )
    summary = {
        "enabled": True,
        "target_queue_id": target_id,
        "target_ready": target_ready,
        "bindings_removed": sorted(removed_ids),
        "jobs_cancelled": cancelled_jobs,
        "followups_cancelled": cancelled_followups,
        "conversations_released": released_conversations,
    }
    # Registrar somente quando houve alteração operacional; chamadas repetidas
    # não devem encher a auditoria nem alterar a fila permitida.
    if changed:
        write_audit(
            db,
            user=None,
            action="AI_EXCLUSIVE_QUEUE_ENFORCED",
            entity_type="service_queue",
            entity_id=target_id,
            payload=summary,
        )
    if changed:
        LOG.warning("ai_exclusive_queue_scope %s", summary)
    return summary
