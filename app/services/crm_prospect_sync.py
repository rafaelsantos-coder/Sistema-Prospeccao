"""Registra no CRM a prospecção feita pela IA assim que o cliente responde.

Antes, só o Vendedor IA criava oportunidade no CRM, e apenas depois do handoff
com nome completo. Conversas em que o cliente respondeu ao SDR (ex.: "Sim",
"Agora não") nunca apareciam como prospecção.

Agora, toda conversa de uma fila com IA em que o cliente respondeu de verdade
gera (uma vez) uma oportunidade na etapa INÍCIO do funil "SDR IA". O CRM
deduplica por externalId, e o Vendedor IA usa o mesmo externalId
(prospecta-conversation-<id>): quando chegar ao fechamento, reaproveita a
mesma oportunidade em vez de criar outra.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIClosingState,
    AIConversationState,
    AuditLog,
    Contact,
    Conversation,
    Lead,
    Message,
)
from app.services.audit import write_audit
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_crm_prospect_sync")

SYNC_ACTION = "CRM_PROSPECT_SYNC"
# Etiqueta no CRM para separar no funil as oportunidades abertas pela IA.
AI_ORIGIN_TAG = "IA"
FAIL_ACTION = "CRM_PROSPECT_SYNC_FAILED"
LOOKBACK = timedelta(days=7)
RETRY_AFTER_FAILURE = timedelta(minutes=30)
BATCH_LIMIT = 20

_AUTOMATED_REPLY = re.compile(
    r"mensagem autom|resposta autom|atendimento autom|no momento nao|"
    r"estamos ausentes|fora do horario|horario de atendimento|retornaremos|"
    r"responderemos assim que|obrigado pelo contato|obrigada pelo contato"
)
_BLOCKED_AI_STATES = {"OPT_OUT"}


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def is_automated_reply(body: str) -> bool:
    """Respostas automáticas de WhatsApp Business não contam como interação."""
    from app.services.auto_reply import is_business_auto_reply  # noqa: PLC0415

    return bool(_AUTOMATED_REPLY.search(_fold(body))) or is_business_auto_reply(body)


def _digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _client_name(conversation: Conversation, contact: Contact, lead: Lead | None) -> str:
    for candidate in (
        getattr(lead, "contact_name", None),
        getattr(lead, "business_name", None),
        contact.name,
    ):
        text = str(candidate or "").strip()
        if text:
            return text[:150]
    return "Cliente WhatsApp"


def _real_reply(db: Session, conversation_id: int) -> Message | None:
    """Última resposta real do cliente (ignora respostas automáticas)."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        )
        .order_by(Message.id.desc())
        .limit(10)
    )
    for message in rows:
        body = (message.body or "").strip()
        if body and not is_automated_reply(body):
            return message
    return None


def _recent_failure(db: Session, conversation_id: int) -> bool:
    return (
        db.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.action == FAIL_ACTION,
                AuditLog.entity_id == str(conversation_id),
                AuditLog.created_at >= utcnow() - RETRY_AFTER_FAILURE,
            )
            .limit(1)
        )
        is not None
    )


def sync_prospects(db: Session, *, limit: int = BATCH_LIMIT) -> int:
    """Cria no CRM as prospecções pendentes. Devolve quantas foram registradas."""
    from app.services.ai_sdr import binding_for_conversation

    since = utcnow() - LOOKBACK
    synced_ids = select(AuditLog.entity_id).where(AuditLog.action == SYNC_ACTION)
    candidates = db.scalars(
        select(Conversation)
        .where(
            Conversation.id.in_(
                select(Message.conversation_id).where(
                    Message.direction == "INBOUND",
                    Message.created_at >= since,
                )
            ),
            Conversation.status.not_in(["BLOQUEADO"]),
        )
        .order_by(Conversation.id.desc())
        .limit(limit * 10)
    )
    done = set(db.scalars(synced_ids))

    client: SulnetCRMClient | None = None
    created = 0
    for conversation in candidates:
        if created >= limit:
            break
        if str(conversation.id) in done or _recent_failure(db, conversation.id):
            continue
        binding = binding_for_conversation(db, conversation.id)
        if not binding or not binding.active:
            continue
        ai_state = db.scalar(
            select(AIConversationState.state).where(
                AIConversationState.conversation_id == conversation.id
            )
        )
        if ai_state in _BLOCKED_AI_STATES:
            continue
        lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
        if lead and lead.do_not_contact:
            continue
        existing = db.scalar(
            select(AIClosingState.opportunity_id).where(
                AIClosingState.conversation_id == conversation.id
            )
        )
        if existing:
            _mark_done(db, conversation.id, existing, "VENDEDOR_JA_CRIOU")
            continue
        contact = conversation.contact
        phone = _digits(contact.phone if contact else "")
        if len(phone) < 10:
            continue
        reply = _real_reply(db, conversation.id)
        if not reply:
            continue

        if client is None:
            client = SulnetCRMClient.from_settings()
        payload = {
            "externalId": f"prospecta-conversation-{conversation.id}",
            "conversationId": conversation.id,
            "leadId": conversation.lead_id,
            "clientName": _client_name(conversation, contact, lead),
            "clientPhone": phone,
            "sdrSummary": (
                "Prospecção pelo SDR IA (WhatsApp). O cliente respondeu: "
                f"\"{(reply.body or '').strip()[:160]}\"."
            ),
            "runConsult": False,
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        try:
            result = client.create_opportunity(payload)
        except CRMIntegrationError as exc:
            write_audit(
                db,
                user=None,
                action=FAIL_ACTION,
                entity_type="conversation",
                entity_id=conversation.id,
                payload={"status": exc.status_code, "error": str(exc)[:200]},
            )
            db.commit()
            LOG.warning(
                "crm_prospect_failed conversation=%s status=%s", conversation.id, exc.status_code
            )
            # CRM fora do ar ou recusando: não insiste com o resto do lote.
            if exc.status_code is None or exc.status_code >= 500:
                break
            continue
        opportunity_id = result.get("opportunityId")
        if opportunity_id:
            try:
                client.add_opportunity_tag(int(opportunity_id), AI_ORIGIN_TAG)
            except (CRMIntegrationError, TypeError, ValueError) as exc:
                LOG.warning("crm_ai_tag_failed opportunity=%s error=%s", opportunity_id, type(exc).__name__)
        if opportunity_id and result.get("created", True):
            try:
                client.add_opportunity_note(
                    int(opportunity_id),
                    result="Primeiro contato",
                    description=(
                        "SDR IA iniciou a conversa pelo WhatsApp. Primeira resposta do cliente: "
                        f"\"{(reply.body or '').strip()[:160]}\"."
                    ),
                )
            except (CRMIntegrationError, TypeError, ValueError) as exc:
                LOG.warning("crm_note_failed opportunity=%s error=%s", opportunity_id, type(exc).__name__)
        _mark_done(db, conversation.id, opportunity_id, "CRIADA" if result.get("created", True) else "JA_EXISTIA")
        created += 1
        LOG.info(
            "crm_prospect_synced conversation=%s opportunity=%s created=%s",
            conversation.id,
            opportunity_id,
            result.get("created", True),
        )
    return created


def synced_opportunity_id(db: Session, conversation_id: int) -> int | None:
    """Oportunidade que o SDR já abriu no CRM para esta conversa, se houver."""
    import json  # noqa: PLC0415

    if db is None:
        return None
    rows = db.scalars(
        select(AuditLog.payload_json)
        .where(AuditLog.action == SYNC_ACTION, AuditLog.entity_id == str(conversation_id))
        .order_by(AuditLog.id.desc())
        .limit(3)
    )
    for raw in rows:
        try:
            value = json.loads(raw or "{}").get("opportunityId")
            if value:
                return int(value)
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def _mark_done(db: Session, conversation_id: int, opportunity_id, outcome: str) -> None:
    write_audit(
        db,
        user=None,
        action=SYNC_ACTION,
        entity_type="conversation",
        entity_id=conversation_id,
        payload={"opportunityId": opportunity_id, "outcome": outcome},
    )
    db.commit()
