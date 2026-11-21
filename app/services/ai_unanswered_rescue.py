"""Resgate pontual, auditavel e sem duplicidade de conversas sem resposta.

Autorizado para os IDs sinalizados pelo monitor em 21/11/2026. Apenas a
ultima mensagem do cliente pode gerar um job; qualquer resposta, bloqueio,
recusa ou tentativa de entrega existente impede novo envio.
"""
from __future__ import annotations

import logging
import unicodedata
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIConversationState, AIJob, AIReplyDelivery,
    AuditLog, Conversation, Message,
)
from app.services.ai_queue_scope import ai_allowed_for_conversation
from app.services.ai_sdr import (
    AI_INBOUND_MESSAGE_TYPES, HUMAN_HANDOFF_STATES,
    binding_for_conversation, enqueue_ai_job, reclaim_conversation_for_ai,
)
from app.services.auto_reply import is_automatic_inbound
from app.services.audit import write_audit
from app.services.ops_alerts import unanswered_customers
from app.services.queue_routing import queue_for_conversation
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_unanswered_rescue")
PRIORITY_CONVERSATION_IDS = (4701, 4018, 1846, 4597)
AUDIT_ACTION = "AI_UNANSWERED_RESCUE_20261121"
TERMINAL_STATES = {"OPT_OUT", "COMPLETED"}


def _defers_or_declines(text: str | None) -> bool:
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold().strip()
    return folded.startswith((
        "agora nao", "nao quero", "sem interesse", "nao tenho interesse",
        "pare de", "nao me envie", "nao entre em contato", "me bloqueie",
    ))


def recover_reported_unanswered(
    db: Session,
    *,
    conversation_ids: tuple[int, ...] = PRIORITY_CONVERSATION_IDS,
) -> dict[str, int]:
    """Coloca um unico job real para cada INBOUND pendente, sem spam nem restart de conversa.

    O controle pelo AuditLog garante que mesmo um novo deploy nao reforce
    mensagens para as quais ja se tentou o resgate. O worker normal entrega
    a resposta e respeita as protecoes adicionais no momento do envio.
    """
    counts: Counter[str] = Counter()
    waiting = {row["conversation_id"]: row for row in unanswered_customers(db)}
    for cid in conversation_ids:
        item = waiting.get(cid)
        if not item:
            counts["not_waiting"] += 1
            LOG.info("ai_priority_rescue_skip conversation=%s reason=not_waiting", cid)
            continue

        conversation = db.get(Conversation, cid)
        queue = queue_for_conversation(db, cid)
        if not conversation or not queue or queue.id not in {1, 2}:
            counts["missing_or_moved"] += 1
            continue
        if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
            counts["closed"] += 1
            continue
        if conversation.lead and (
            conversation.lead.do_not_contact
            or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
        ):
            counts["do_not_contact"] += 1
            continue
        if not ai_allowed_for_conversation(db, cid):
            counts["not_authorized"] += 1
            continue

        # Se houve uma resposta, até um envio pendente ou incerto, não criar
        # uma segunda tentativa. Falha definitiva de entrega exige tratamento
        # da própria AIReplyDelivery, nunca outro texto duplicado.
        latest = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == cid,
                Message.direction.in_(["INBOUND", "OUTBOUND"]),
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        if not latest or latest.direction != "INBOUND":
            counts["outbound_exists"] += 1
            continue
        if latest.message_type not in AI_INBOUND_MESSAGE_TYPES or is_automatic_inbound(latest):
            counts["unsupported_or_automatic"] += 1
            continue
        if _defers_or_declines(latest.body):
            counts["declined"] += 1
            continue

        state = db.scalar(select(AIConversationState).where(
            AIConversationState.conversation_id == cid
        ))
        if state and state.state in TERMINAL_STATES:
            counts["terminal"] += 1
            continue
        prior_delivery = db.scalar(select(AIReplyDelivery.id).where(
            AIReplyDelivery.inbound_message_id == latest.id
        ).limit(1))
        if prior_delivery:
            counts["delivery_exists"] += 1
            continue
        already_requested = db.scalar(select(AuditLog.id).where(
            AuditLog.action == AUDIT_ACTION,
            AuditLog.entity_type == "message",
            AuditLog.entity_id == str(latest.id),
        ).limit(1))
        if already_requested:
            counts["already_rescued"] += 1
            continue

        prior_job = db.scalar(select(AIJob).where(
            AIJob.message_id == latest.id
        ).limit(1))
        if prior_job and prior_job.status in {"PENDENTE", "PROCESSANDO"}:
            counts["job_already_active"] += 1
            continue

        binding = binding_for_conversation(db, cid)
        if not binding or not binding.agent or not binding.agent.active:
            counts["no_agent"] += 1
            continue

        # A ultima mensagem e do cliente ha >10 min sem resposta; para ESTES
        # quatro IDs explicitamente delegados, liberar posse humana antiga.
        # Uma resposta humana posterior nao passaria pela guarda de "latest".
        former_owner = conversation.assigned_user_id
        conversation.assigned_user_id = None
        if state and state.state in HUMAN_HANDOFF_STATES:
            if not reclaim_conversation_for_ai(db, conversation, state):
                conversation.assigned_user_id = former_owner
                counts["handoff_not_reclaimable"] += 1
                continue

        if state and state.state == "CLOSING":
            agent = db.get(AIAgentProfile, state.agent_id)
            if not agent or not agent.active or agent.role_type != "CLOSER":
                conversation.assigned_user_id = former_owner
                counts["closer_unavailable"] += 1
                continue
        else:
            agent = binding.agent

        if prior_job:
            prior_job.agent_id = agent.id
            prior_job.queue_id = queue.id
            prior_job.status = "PENDENTE"
            prior_job.available_at = utcnow()
            prior_job.locked_at = None
            prior_job.attempts = 0
            prior_job.error = AUDIT_ACTION
            job = prior_job
        else:
            job = enqueue_ai_job(db, conversation=conversation, message=latest, takeover=True)
            if not job:
                conversation.assigned_user_id = former_owner
                counts["enqueue_blocked"] += 1
                continue
            job.available_at = utcnow()

        # Evita que um registro antigo de atribuicao humana bloqueie a
        # entrega, mesmo quando a ultima mensagem dessa pessoa foi ANTES do
        # ultimo INBOUND do cliente.
        conversation.assigned_user_id = agent.user_id
        conversation.status = "EM_ATENDIMENTO"
        write_audit(
            db, user=None, action=AUDIT_ACTION, entity_type="message",
            entity_id=latest.id,
            payload={
                "conversation_id": cid,
                "queue_id": queue.id,
                "agent_id": agent.id,
                "job_id": job.id,
                "previous_owner": former_owner,
            },
        )
        db.flush()
        counts["requeued" if prior_job else "enqueued"] += 1
        LOG.warning(
            "ai_priority_rescue_enqueued conversation=%s inbound=%s job=%s queue=%s previous_owner=%s",
            cid, latest.id, job.id, queue.id, former_owner,
        )
    LOG.warning("ai_priority_rescue_summary %s", dict(counts))
    return dict(counts)
