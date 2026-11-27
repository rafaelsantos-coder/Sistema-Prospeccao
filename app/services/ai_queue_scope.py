"""Politicas de autorizacao da IA por fila.

Em producao a SulIA atua exclusivamente na Fila IA, sem excecoes por
origem manual, posse anterior, fila de job ou configuracao legada.
A ativacao do vinculo dedicado continua sob controle do gestor.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

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
from app.config import settings

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


def is_general_commercial_queue(queue: ServiceQueue | None) -> bool:
    """Identifica a fila Geral Comercial para autorizacao explicita da SulIA."""
    if queue is None:
        return False
    raw_name = str(getattr(queue, "name", "") or "")
    raw_code = str(getattr(queue, "code", "") or "")
    normalized_name = unicodedata.normalize("NFKD", raw_name)
    normalized_name = "".join(
        c for c in normalized_name if not unicodedata.combining(c)
    )
    name_words = set(re.findall(r"[a-z0-9]+", normalized_name.casefold()))
    code_words = set(re.findall(r"[a-z0-9]+", raw_code.casefold()))
    return (
        {"geral", "comercial"}.issubset(name_words)
        or {"geral", "comercial"}.issubset(code_words)
        or raw_code.strip().upper() in {"FILA-GERAL", "FILA_GERAL", "GERAL"}
    )


def dedicated_queue_only() -> bool:
    """Regra obrigatoria em producao; opt-in para simulacoes locais."""
    return (
        settings.is_production
        or os.getenv("RAILWAY_ENVIRONMENT_NAME", "").strip().lower() == "production"
        or os.getenv("AI_DEDICATED_QUEUE_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
    )


def dedicated_ai_queue(db: Session) -> ServiceQueue | None:
    """Resolve a unica Fila IA ativa; preferir o nome operacional ao codigo legado."""
    queues = list(db.scalars(select(ServiceQueue).where(ServiceQueue.active.is_(True))))
    by_name = [
        queue for queue in queues
        if " ".join(str(queue.name or "").replace("-", " ").replace("_", " ").casefold().split()) == "fila ia"
    ]
    candidates = by_name or [queue for queue in queues if is_dedicated_ai_queue(queue)]
    return candidates[0] if len(candidates) == 1 else None


def manual_queue_control_enabled() -> bool:
    return dedicated_queue_only() or os.getenv("AI_QUEUE_MANUAL_CONTROL", "false").strip().lower() in {"1", "true", "yes", "on"}


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


def ai_allowed_for_queue(queue_id: int | None, *, db: Session | None = None) -> bool:
    """Fila elegivel; em producao exige identificacao positiva no banco."""
    if dedicated_queue_only():
        if not queue_id or db is None:
            return False
        target = dedicated_ai_queue(db)
        return bool(target and target.id == queue_id)
    if manual_queue_control_enabled():
        return queue_id is not None and int(queue_id) > 0
    exclusive = exclusive_ai_queue_id()
    return exclusive is None or bool(queue_id is not None and int(queue_id) == exclusive)


def ai_queue_enabled(db: Session, queue_id: int | None) -> bool:
    """Autorizacao efetiva por vinculo ativo e aprovado pelo gestor."""
    if not ai_allowed_for_queue(queue_id, db=db):
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


def manual_ai_handoff_authorized(db: Session, conversation_id: int) -> bool:
    """Autorizacao EXCLUSIVA da conversa que um humano passou a SulIA.

    Nao habilita IA automatica na fila humana. Exige vinculo SDR ativo na
    Fila IA, mas nao depende da sua instancia WhatsApp estar conectada.
    """
    if not hasattr(db, "scalar") or not hasattr(db, "get"):
        return False
    link = db.scalar(
        select(ConversationQueueLink).where(
            ConversationQueueLink.conversation_id == conversation_id
        )
    )
    if not link or getattr(link, "origin", None) != "MANUAL_AI":
        # Doubles de testes antigos podem retornar objetos simplificados.
        return False
    queue = db.get(ServiceQueue, link.queue_id)
    if not queue or not queue.active:
        return False
    if dedicated_queue_only():
        return ai_queue_enabled(db, link.queue_id)
    from app.services.queue_routing import ai_transfer_queue  # noqa: PLC0415

    return ai_transfer_queue(db) is not None


def ai_allowed_for_conversation(
    db: Session,
    conversation_id: int,
    *,
    fallback_queue_id: int | None = None,
) -> bool:
    if dedicated_queue_only():
        # Nunca confiar no queue_id antigo do job nem na marca MANUAL_AI.
        # Ler o vinculo corrente, mesmo apos transferencias durante o processamento.
        current = db.scalar(
            select(ConversationQueueLink.queue_id).where(
                ConversationQueueLink.conversation_id == conversation_id
            )
        )
        return ai_queue_enabled(db, current) if current is not None else False
    # Doubles simples de testes antigos não expõem toda a interface de Session.
    # Em produção, Session real sempre executa o bloqueio por fila abaixo.
    if (
        not manual_queue_control_enabled()
        and exclusive_ai_queue_id() is None
        and (not hasattr(db, "scalar") or not hasattr(db, "get"))
    ):
        return True
    current = db.scalar(
        select(ConversationQueueLink.queue_id).where(
            ConversationQueueLink.conversation_id == conversation_id
        )
    )
    queue_id = current if current is not None else fallback_queue_id
    queue = db.get(ServiceQueue, queue_id) if queue_id and hasattr(db, "get") else None
    if manual_ai_handoff_authorized(db, conversation_id):
        return True
    # Na Geral Comercial, uma fala humana recente protege a conversa sem
    # posse efetiva de IA. Uma conversa já entregue à IA não volta à triagem
    # por causa de uma fala humana anterior à entrega.
    if is_general_commercial_queue(queue):
        from app.services.ai_sdr import human_stepped_in, human_is_attending

        conversation = db.get(Conversation, conversation_id)
        if conversation is not None:
            if human_is_attending(db, conversation):
                return False
            if human_stepped_in(db, conversation, window=timedelta(hours=24)):
                state = db.scalar(select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation_id
                ))
                owner = db.get(AIAgentProfile, state.agent_id) if state and state.agent_id else None
                owned = bool(
                    state and state.state in {"AI_ACTIVE", "CLOSING"}
                    and owner and owner.active and owner.user_id
                    and owner.user_id == conversation.assigned_user_id
                )
                if not owned:
                    return False
    if not manual_queue_control_enabled() and exclusive_ai_queue_id() is None:
        return True
    return ai_queue_enabled(db, queue_id) if manual_queue_control_enabled() else ai_allowed_for_queue(queue_id)


def enforce_exclusive_ai_queue(db: Session) -> dict[str, object]:
    """Reconciliar vínculos e pendências sem excluir histórico de mensagens.

    A fila permitida continua com SDR ativo. Vínculos IA de outras filas são
    removidos, jobs pendentes cancelados e conversas de posse da IA são
    devolvidas à fila humana. Operação idempotente e auditável.
    """
    strict = dedicated_queue_only()
    if strict:
        target = dedicated_ai_queue(db)
        target_id = target.id if target is not None else 0
    else:
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
            if strict:
                # Preserva cadastro e auditoria, mas remove toda autorizacao indevida.
                if binding.active or binding.manual_authorized or binding.handling_mode != "HUMAN":
                    removed_ids.append(binding.queue_id)
                    binding.active = False
                    binding.manual_authorized = False
                    binding.handling_mode = "HUMAN"
            else:
                removed_ids.append(binding.queue_id)
                db.delete(binding)

    target_ready = bool(target and target.active)
    if strict:
        # Nunca reativar um agente/fila pausado pelo gestor.
        target_ready = bool(target_ready and ai_queue_enabled(db, target_id))
    elif target_ready:
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
    if not strict:
        not_target = not_target & or_(
            ConversationQueueLink.origin != "MANUAL_AI",
            ConversationQueueLink.origin.is_(None),
        )
    revoked_delegations = 0
    if strict:
        for link in db.scalars(select(ConversationQueueLink).where(
            ConversationQueueLink.queue_id != target_id,
            ConversationQueueLink.origin == "MANUAL_AI",
        )):
            link.origin = "MANUAL_TRANSFER"
            revoked_delegations += 1
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
        or target_changed or revoked_delegations
    )
    summary = {
        "enabled": True,
        "target_queue_id": target_id,
        "target_ready": target_ready,
        "bindings_removed": sorted(removed_ids),
        "jobs_cancelled": cancelled_jobs,
        "followups_cancelled": cancelled_followups,
        "conversations_released": released_conversations,
        "delegations_revoked": revoked_delegations,
        "dedicated_only": strict,
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
