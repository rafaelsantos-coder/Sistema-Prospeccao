"""Resgate pontual, auditavel e sem duplicidade de conversas sem resposta.

Autorizado para os IDs sinalizados pelo monitor em 21/11/2026. Apenas a
ultima mensagem do cliente pode gerar um job; qualquer resposta, bloqueio,
recusa ou tentativa de entrega existente impede novo envio.
"""
from __future__ import annotations

import logging
import unicodedata
from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIConversationState, AIJob, AIReplyDelivery,
    AuditLog, Conversation, ConversationQueueLink, Message,
)
from app.services.ai_queue_scope import ai_allowed_for_conversation, ai_queue_enabled, human_takeover_locked, is_general_commercial_queue
from app.services.ai_sdr import (
    AI_INBOUND_MESSAGE_TYPES, HUMAN_HANDOFF_STATES,
    binding_for_conversation, enqueue_ai_job, reclaim_conversation_for_ai,
)
from app.services.auto_reply import is_automatic_inbound
from app.services.audit import write_audit
from app.services.ops_alerts import unanswered_customers
from app.services.queue_routing import queue_for_conversation, ai_transfer_queue, MANUAL_AI_ORIGIN
from app.timeutils import utc_naive, utcnow


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
        # Só o resgate pontual dos IDs solicitados pode registrar uma delegação
        # explícita. Não permitir esse caminho se a fila estiver desautorizada.
        if human_takeover_locked(db, cid):
            counts["human_takeover"] += 1
            continue
        needs_delegation = not ai_allowed_for_conversation(db, cid)
        if needs_delegation and not (
            is_general_commercial_queue(queue)
            and ai_queue_enabled(db, queue.id)
            and ai_transfer_queue(db) is not None
        ):
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
            if latest:
                LOG.warning(
                    "ai_priority_rescue_outbound_state conversation=%s outbound=%s "
                    "status=%s has_provider=%s has_sent_at=%s",
                    cid, latest.id, latest.status,
                    bool(latest.provider_message_id or latest.provider_zaap_id),
                    bool(latest.sent_at),
                )
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

        # A recuperação autorizada é individual, após verificar a última
        # mensagem, ausência de entrega, recusa e estado terminal.
        # A fila e o número de origem não mudam.
        delegated_link = None
        former_origin = None
        if needs_delegation:
            received = utc_naive(latest.created_at)
            if not received or utcnow() - received < timedelta(minutes=10):
                counts["not_authorized"] += 1
                continue
            delegated_link = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == cid
            ))
            if not delegated_link:
                counts["not_authorized"] += 1
                continue
            former_origin = delegated_link.origin
            delegated_link.origin = MANUAL_AI_ORIGIN
            db.flush()

        binding = binding_for_conversation(db, cid)
        if not binding or not binding.agent or not binding.agent.active:
            if delegated_link is not None:
                delegated_link.origin = former_origin
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
                if delegated_link is not None:
                    delegated_link.origin = former_origin
                counts["handoff_not_reclaimable"] += 1
                continue

        if state and state.state == "CLOSING":
            agent = db.get(AIAgentProfile, state.agent_id)
            if not agent or not agent.active or agent.role_type != "CLOSER":
                conversation.assigned_user_id = former_owner
                if delegated_link is not None:
                    delegated_link.origin = former_origin
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
                if delegated_link is not None:
                    delegated_link.origin = former_origin
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
                "explicit_delegation": needs_delegation,
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



RETRY_AUDIT_ACTION = "AI_PRIORITY_JOB_RETRY_20261121"
DELIVERY_RETRY_ACTION = "AI_PRIORITY_DELIVERY_RETRY_20261121"


def recover_priority_stalled_jobs(db: Session) -> dict[str, int]:
    """Recupera UMA vez jobs dos quatro resgates que falharam ou ficaram travados.

    Para PROCESSANDO exige lock parado por 5 min, porque um modelo de IA
    ainda pode estar trabalhando. Nunca recria job ou mensagem existente.
    """
    counts: Counter[str] = Counter()
    now = utcnow()
    for cid in PRIORITY_CONVERSATION_IDS:
        conversation = db.get(Conversation, cid)
        queue = queue_for_conversation(db, cid)
        if not conversation or not queue or queue.id not in {1, 2}:
            continue
        if conversation.status in {"ENCERRADO", "RESOLVIDO", "BLOQUEADO"}:
            continue
        if conversation.lead and (
            conversation.lead.do_not_contact
            or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
        ):
            continue
        if not ai_allowed_for_conversation(db, cid):
            continue
        state = db.scalar(select(AIConversationState).where(
            AIConversationState.conversation_id == cid
        ))
        if state and state.state in TERMINAL_STATES:
            continue

        latest = db.scalar(select(Message).where(
            Message.conversation_id == cid,
            Message.direction.in_(["INBOUND", "OUTBOUND"]),
        ).order_by(Message.id.desc()).limit(1))
        if not latest or latest.direction != "INBOUND":
            continue
        if latest.message_type not in AI_INBOUND_MESSAGE_TYPES or is_automatic_inbound(latest):
            continue
        if _defers_or_declines(latest.body):
            continue

        job = db.scalar(select(AIJob).where(AIJob.message_id == latest.id).limit(1))
        if not job:
            continue
        counts["seen_status_" + str(job.status).lower()] += 1
        if job.status == "PROCESSANDO":
            lock_at = utc_naive(job.locked_at)
            if not lock_at or now - lock_at < timedelta(minutes=5):
                continue
        elif job.status not in {"FALHOU", "IGNORADO", "CANCELADO"}:
            continue
        if db.scalar(select(AIReplyDelivery.id).where(
            AIReplyDelivery.inbound_message_id == latest.id
        ).limit(1)):
            counts["existing_delivery"] += 1
            continue
        if db.scalar(select(AuditLog.id).where(
            AuditLog.action == RETRY_AUDIT_ACTION,
            AuditLog.entity_type == "message",
            AuditLog.entity_id == str(latest.id),
        ).limit(1)):
            counts["retry_already_requested"] += 1
            continue

        binding = binding_for_conversation(db, cid)
        if not binding or not binding.agent or not binding.agent.active:
            counts["binding_missing"] += 1
            continue
        agent = binding.agent
        if state and state.state == "CLOSING":
            agent = db.get(AIAgentProfile, state.agent_id)
            if not agent or not agent.active or agent.role_type != "CLOSER":
                counts["closer_missing"] += 1
                continue

        job.agent_id = agent.id
        job.queue_id = queue.id
        job.status = "PENDENTE"
        job.available_at = now
        job.locked_at = None
        job.attempts = 0
        job.error = RETRY_AUDIT_ACTION
        conversation.assigned_user_id = agent.user_id
        conversation.status = "EM_ATENDIMENTO"
        write_audit(
            db, user=None, action=RETRY_AUDIT_ACTION,
            entity_type="message", entity_id=latest.id,
            payload={"conversation_id": cid, "job_id": job.id},
        )
        counts["retried"] += 1
        LOG.warning(
            "ai_priority_job_retried conversation=%s inbound=%s job=%s",
            cid, latest.id, job.id,
        )
    LOG.warning("ai_priority_stalled_summary %s", dict(counts))
    return dict(counts)


def retry_known_failed_priority_delivery(db: Session) -> dict[str, int]:
    """Reativa a entrega original, nunca cria outro texto para o mesmo cliente.

    Só falha CONFIRMADA sem protocolo do provedor pode ser reenviada. Uma
    tentativa UNKNOWN jamais deve ser duplicada automaticamente.
    """
    from app.services.ai_reply_delivery import FAILED_STATUSES, MAX_ATTEMPTS, TEXT_RESCUE_MARKER

    counts: Counter[str] = Counter()
    for cid in PRIORITY_CONVERSATION_IDS:
        conversation = db.get(Conversation, cid)
        if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
            continue
        if conversation.lead and (
            conversation.lead.do_not_contact
            or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
        ):
            continue
        if not ai_allowed_for_conversation(db, cid):
            continue
        latest = db.scalar(select(Message).where(
            Message.conversation_id == cid,
            Message.direction.in_(["INBOUND", "OUTBOUND"]),
        ).order_by(Message.id.desc()).limit(1))
        if not latest or latest.direction != "OUTBOUND":
            continue
        counts["seen_outbound_" + str(latest.status).lower()] += 1
        if latest.status not in FAILED_STATUSES:
            continue
        if latest.provider_message_id or latest.provider_zaap_id or latest.sent_at or latest.delivered_at:
            counts["provider_uncertain"] += 1
            continue
        delivery = db.scalar(select(AIReplyDelivery).where(
            AIReplyDelivery.outbound_message_id == latest.id
        ).limit(1))
        if not delivery:
            counts["no_retryable_delivery"] += 1
            continue
        if delivery.attempts >= MAX_ATTEMPTS:
            # O cliente pediu expressamente o resgate. A ultima saida
            # comprovadamente falhou sem protocolo/recibo; conceder uma
            # ultima sequencia de envio, nunca repetir depois do marcador.
            if cid != 4597:
                counts["exhausted"] += 1
                continue
            from app.services.ai_sdr import human_is_attending

            if human_is_attending(db, conversation):
                counts["human_is_attending"] += 1
                continue
            source = db.get(Message, delivery.inbound_message_id)
            if not source or is_automatic_inbound(source) or _defers_or_declines(source.body):
                counts["source_not_eligible"] += 1
                continue
            state = db.scalar(select(AIConversationState).where(
                AIConversationState.conversation_id == cid
            ))
            if state and state.state in TERMINAL_STATES:
                counts["terminal"] += 1
                continue
            agent = db.get(AIAgentProfile, delivery.agent_id)
            if not agent or not agent.active:
                counts["agent_inactive"] += 1
                continue
            if state is None:
                state = AIConversationState(
                    conversation_id=cid, agent_id=agent.id, state="AI_ACTIVE",
                )
                db.add(state)
            elif state.state in {"NEW", "SHADOW_REVIEW"}:
                state.state = "AI_ACTIVE"
                state.agent_id = agent.id
            elif state.state in {"AI_ACTIVE", "CLOSING"} and state.agent_id == agent.id:
                pass
            else:
                counts["owner_mismatch"] += 1
                continue
            conversation.assigned_user_id = agent.user_id
            conversation.status = "EM_ATENDIMENTO"
            delivery.attempts = 0
            # Prefere texto puro se o provedor rejeitou repetidamente o menu
            # de botoes; nunca perde o conteudo original.
            if delivery.buttons_json not in {"", "[]"} and not delivery.link_url:
                delivery.link_label = TEXT_RESCUE_MARKER
            counts["exhausted_reset_once"] += 1
        if db.scalar(select(AuditLog.id).where(
            AuditLog.action == DELIVERY_RETRY_ACTION,
            AuditLog.entity_type == "message",
            AuditLog.entity_id == str(latest.id),
        ).limit(1)):
            continue
        delivery.available_at = utcnow()
        delivery.locked_at = None
        write_audit(
            db, user=None, action=DELIVERY_RETRY_ACTION,
            entity_type="message", entity_id=latest.id,
            payload={"conversation_id": cid, "attempts_before": delivery.attempts},
        )
        counts["delivery_retry_queued"] += 1
        LOG.warning(
            "ai_priority_delivery_retried conversation=%s outbound=%s attempts=%s",
            cid, latest.id, delivery.attempts,
        )
    LOG.warning("ai_priority_delivery_summary %s", dict(counts))
    return dict(counts)



ORPHAN_FAILED_ACTION = "AI_ORPHAN_FAILED_OUTBOUND_RESCUE_20261121"


def rescue_orphan_failed_outbound(db: Session, *, conversation_ids: tuple[int, ...] = (4597,)) -> dict[str, int]:
    """Retoma uma ultima saida comprovadamente falha que nao tem AIReplyDelivery.

    A falha sem provider_message_id, zaapId, sent_at ou delivered_at prova que
    a ultima tentativa nao foi aceita. A IA responde a ultima mensagem real do
    cliente, nunca a uma mensagem automatica e nunca se houver saida confirmada
    depois dela. Trata somente a conversa solicitada pela gestao.
    """
    from app.services.ai_reply_delivery import DELIVERED_STATUSES, FAILED_STATUSES
    from app.services.ai_sdr import human_is_attending

    counts: Counter[str] = Counter()
    for cid in conversation_ids:
        # O monitor pode ignorar a saida falha se ela nao tiver sido gerada
        # pelo fluxo AIReplyDelivery; a prova e o FAILED sem recibo e sem
        # outra saida efetivamente enviada.
        conversation = db.get(Conversation, cid)
        queue = queue_for_conversation(db, cid)
        if not conversation or not queue or queue.id != 2:
            counts["missing_or_moved"] += 1
            continue
        if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
            counts["closed"] += 1
            continue
        if conversation.lead and (
            conversation.lead.do_not_contact
            or conversation.lead.stage in {"GANHO", "NAO_CONTATAR"}
        ):
            counts["blocked_lead"] += 1
            continue
        if not ai_allowed_for_conversation(db, cid) or human_is_attending(db, conversation):
            counts["not_authorized_or_human"] += 1
            continue

        latest = db.scalar(select(Message).where(
            Message.conversation_id == cid,
            Message.direction.in_(["INBOUND", "OUTBOUND"]),
        ).order_by(Message.id.desc()).limit(1))
        if (
            not latest or latest.direction != "OUTBOUND"
            or latest.status not in FAILED_STATUSES
            or latest.provider_message_id or latest.provider_zaap_id
            or latest.sent_at or latest.delivered_at or latest.read_at
        ):
            counts["not_proven_failed"] += 1
            continue
        if db.scalar(select(AuditLog.id).where(
            AuditLog.action == ORPHAN_FAILED_ACTION,
            AuditLog.entity_type == "message",
            AuditLog.entity_id == str(latest.id),
        ).limit(1)):
            counts["already_rescued"] += 1
            continue
        if db.scalar(select(AIReplyDelivery.id).where(
            AIReplyDelivery.outbound_message_id == latest.id
        ).limit(1)):
            counts["existing_delivery"] += 1
            continue
        inbound = db.scalar(select(Message).where(
            Message.conversation_id == cid,
            Message.direction == "INBOUND",
            Message.id < latest.id,
        ).order_by(Message.id.desc()).limit(1))
        if (
            not inbound or inbound.message_type not in AI_INBOUND_MESSAGE_TYPES
            or is_automatic_inbound(inbound) or _defers_or_declines(inbound.body)
        ):
            counts["inbound_not_eligible"] += 1
            continue
        # Nenhuma outra resposta a essa mensagem pode estar em trânsito.
        if db.scalar(select(AIReplyDelivery.id).where(
            AIReplyDelivery.inbound_message_id == inbound.id
        ).limit(1)):
            counts["source_delivery_exists"] += 1
            continue
        acknowledged = db.scalar(select(Message.id).where(
            Message.conversation_id == cid,
            Message.direction == "OUTBOUND",
            Message.id > inbound.id,
            (
                Message.status.in_(DELIVERED_STATUSES | {"SENT"})
                | Message.provider_message_id.is_not(None)
                | Message.provider_zaap_id.is_not(None)
                | Message.sent_at.is_not(None)
                | Message.delivered_at.is_not(None)
            ),
        ).limit(1))
        if acknowledged:
            counts["another_reply_exists"] += 1
            continue
        state = db.scalar(select(AIConversationState).where(
            AIConversationState.conversation_id == cid
        ))
        if state and state.state in TERMINAL_STATES:
            counts["terminal"] += 1
            continue
        binding = binding_for_conversation(db, cid)
        if not binding or not binding.agent or not binding.agent.active:
            counts["no_binding"] += 1
            continue
        agent = binding.agent
        if state and state.state == "CLOSING":
            agent = db.get(AIAgentProfile, state.agent_id)
            if not agent or not agent.active or agent.role_type != "CLOSER":
                counts["closer_not_ready"] += 1
                continue

        prior_job = db.scalar(select(AIJob).where(
            AIJob.message_id == inbound.id
        ).limit(1))
        if prior_job and prior_job.status in {"PENDENTE", "PROCESSANDO"}:
            counts["job_in_progress"] += 1
            continue

        if prior_job:
            prior_job.agent_id = agent.id
            prior_job.queue_id = queue.id
            prior_job.status = "PENDENTE"
            prior_job.available_at = utcnow()
            prior_job.locked_at = None
            prior_job.attempts = 0
            prior_job.error = ORPHAN_FAILED_ACTION
            job = prior_job
        else:
            job = enqueue_ai_job(
                db, conversation=conversation, message=inbound, takeover=True,
            )
            if not job:
                counts["enqueue_blocked"] += 1
                continue
            job.available_at = utcnow()
        conversation.assigned_user_id = agent.user_id
        conversation.status = "EM_ATENDIMENTO"
        write_audit(
            db, user=None, action=ORPHAN_FAILED_ACTION,
            entity_type="message", entity_id=latest.id,
            payload={
                "conversation_id": cid, "inbound_id": inbound.id, "job_id": job.id,
                "failed_outbound_id": latest.id,
            },
        )
        counts["requeued" if prior_job else "enqueued"] += 1
        LOG.warning(
            "ai_orphan_failed_outbound_rescued conversation=%s failed=%s inbound=%s job=%s",
            cid, latest.id, inbound.id, job.id,
        )
    LOG.warning("ai_orphan_failed_outbound_summary %s", dict(counts))
    return dict(counts)
