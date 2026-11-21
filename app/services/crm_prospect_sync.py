"""Registra no CRM a oportunidade assim que a SulIA inicia o atendimento.

Oportunidade única por conversa, reutilizada no fechamento e recuperada em falhas.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile,
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


def ai_crm_opportunity_id(db: Session, conversation_id: int) -> int | None:
    """Identificador único que SDR e Vendedor IA devem reutilizar."""
    closing_id = db.scalar(select(AIClosingState.opportunity_id).where(
        AIClosingState.conversation_id == conversation_id,
    ))
    return int(closing_id) if closing_id else synced_opportunity_id(db, conversation_id)


def _ai_authored_message(db: Session, conversation_id: int) -> Message | None:
    """Disparos da campanha não são mensagens da SulIA."""
    ai_users = select(AIAgentProfile.user_id)
    return db.scalar(select(Message).where(
        Message.conversation_id == conversation_id,
        Message.direction == "OUTBOUND",
        Message.sender_user_id.in_(ai_users),
    ).order_by(Message.id.asc()).limit(1))


def _assigned_to_ai(db: Session, conversation: Conversation) -> bool:
    if not conversation.assigned_user_id:
        return False
    return db.scalar(select(AIAgentProfile.id).where(
        AIAgentProfile.user_id == conversation.assigned_user_id,
        AIAgentProfile.active.is_(True),
    ).limit(1)) is not None


def _previous_uncertain(db: Session, conversation_id: int) -> bool:
    """Timeout anterior pode ter criado a oportunidade antes de falhar."""
    rows = db.scalars(select(AuditLog.payload_json).where(
        AuditLog.action == FAIL_ACTION,
        AuditLog.entity_id == str(conversation_id),
    ).order_by(AuditLog.id.desc()).limit(15))
    for raw in rows:
        try:
            if json.loads(raw or "{}").get("uncertain"):
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


def _recover_uncertain_from_crm(
    client: SulnetCRMClient, conversation_id: int, phone: str,
) -> int | None:
    """Reutiliza somente um resultado ligado explicitamente à conversa."""
    from app.services.crm_manual_registration import (
        CRMManualError, _phone_key, _record_id, _record_phone, _records,
    )

    if not client.bearer_token:
        raise CRMIntegrationError("CRM sem consulta de reconciliação.")
    try:
        records = _records(client.find_opportunities_by_phone(phone))
    except CRMManualError as exc:
        raise CRMIntegrationError("CRM não confirmou pesquisa de registro incerto.") from exc

    external_id = f"prospecta-conversation-{conversation_id}"
    for record in records:
        if _phone_key(_record_phone(record)) != _phone_key(phone):
            continue
        identity = record.get("externalId") or record.get("external_id")
        origin_conversation = record.get("conversationId") or record.get("conversation_id")
        if identity == external_id or str(origin_conversation or "") == str(conversation_id):
            opportunity_id = _record_id(record)
            if opportunity_id:
                return opportunity_id
    return None


def sync_prospects(
    db: Session, *, limit: int = BATCH_LIMIT,
    conversation_id: int | None = None, force: bool = False,
) -> int:
    """Inicia cadastro no primeiro contato IA e não repete o POST confirmado.

    O processamento da primeira mensagem chama a sincronização imediata. O
    worker também reconcilia periodicamente. A opção force permite iniciar
    pelo botão manual somente quando a conversa pertence à IA.
    """
    from app.services.ai_sdr import binding_for_conversation

    since = utcnow() - LOOKBACK
    if conversation_id is not None:
        query = select(Conversation).where(Conversation.id == conversation_id)
    else:
        ai_users = select(AIAgentProfile.user_id)
        ai_started = select(Message.conversation_id).where(
            Message.direction == "OUTBOUND",
            Message.sender_user_id.in_(ai_users),
            Message.created_at >= since,
        )
        query = (
            select(Conversation)
            .where(Conversation.id.in_(ai_started))
            .order_by(Conversation.id.desc())
            .limit(limit * 10)
        )
    candidates = list(db.scalars(query))
    done = 0
    client: SulnetCRMClient | None = None
    for candidate in candidates:
        if done >= limit:
            break
        # Serializa o agendador, o worker e o botão manual, sem requisições duplicadas.
        conversation = db.scalar(
            select(Conversation).where(Conversation.id == candidate.id).with_for_update()
        )
        if not conversation or conversation.status == "BLOQUEADO":
            continue
        binding = binding_for_conversation(db, conversation.id)
        if not binding or not binding.active:
            continue
        ai_state = db.scalar(select(AIConversationState.state).where(
            AIConversationState.conversation_id == conversation.id
        ))
        if ai_state in _BLOCKED_AI_STATES:
            continue
        lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
        if lead and lead.do_not_contact:
            continue
        started = _ai_authored_message(db, conversation.id)
        if not started and not (force and _assigned_to_ai(db, conversation)):
            continue

        recorded = synced_opportunity_id(db, conversation.id)
        if recorded:
            continue
        existing = db.scalar(select(AIClosingState.opportunity_id).where(
            AIClosingState.conversation_id == conversation.id
        ))
        if existing:
            _mark_done(db, conversation.id, existing, "VENDEDOR_JA_CRIOU")
            done += 1
            continue
        if _recent_failure(db, conversation.id):
            continue

        contact = conversation.contact
        phone = _digits(contact.phone if contact else "")
        if len(phone) < 10:
            continue
        reply = _real_reply(db, conversation.id)
        if client is None:
            client = SulnetCRMClient.from_settings()

        # Não repetir um POST que pode ter sido efetivado durante um timeout.
        if _previous_uncertain(db, conversation.id):
            try:
                recovered_id = _recover_uncertain_from_crm(client, conversation.id, phone)
            except CRMIntegrationError as exc:
                LOG.warning("crm_prospect_uncertain_lookup_failed conversation=%s status=%s",
                            conversation.id, exc.status_code)
                continue
            if recovered_id:
                _mark_done(db, conversation.id, recovered_id, "RECONCILIADO")
                done += 1
                continue
            LOG.warning("crm_prospect_uncertain_requires_review conversation=%s", conversation.id)
            continue

        summary = (
            f'Prospecção SulIA (WhatsApp). Cliente respondeu: "{(reply.body or "").strip()[:160]}".'
            if reply else "Atendimento comercial iniciado pela SulIA no WhatsApp."
        )
        payload = {
            "externalId": f"prospecta-conversation-{conversation.id}",
            "conversationId": conversation.id,
            "leadId": conversation.lead_id,
            "clientName": _client_name(conversation, contact, lead),
            "clientPhone": phone,
            "sdrSummary": summary,
            "runConsult": False,
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        try:
            result = client.create_opportunity(payload)
            opportunity_id = result.get("opportunityId")
            if not opportunity_id:
                raise CRMIntegrationError("CRM não confirmou a oportunidade.")
        except CRMIntegrationError as exc:
            uncertain = exc.status_code is None or exc.status_code >= 500
            write_audit(db, user=None, action=FAIL_ACTION, entity_type="conversation",
                        entity_id=conversation.id, payload={
                            "status": exc.status_code, "uncertain": uncertain,
                            "error": str(exc)[:200],
                        })
            db.commit()
            LOG.warning("crm_prospect_failed conversation=%s status=%s uncertain=%s",
                        conversation.id, exc.status_code, uncertain)
            if uncertain:
                break
            continue

        # Primeiro persistir o ID; falha ao adicionar tag ou nota não duplica.
        _mark_done(
            db, conversation.id, opportunity_id,
            "CRIADA" if result.get("created", True) else "JA_EXISTIA",
        )
        done += 1
        try:
            client.add_opportunity_tag(int(opportunity_id), AI_ORIGIN_TAG)
        except (CRMIntegrationError, TypeError, ValueError) as exc:
            LOG.warning("crm_ai_tag_failed opportunity=%s error=%s",
                        opportunity_id, type(exc).__name__)
        if result.get("created", True):
            try:
                client.add_opportunity_note(
                    int(opportunity_id), result="Primeiro contato",
                    description=summary,
                )
            except (CRMIntegrationError, TypeError, ValueError) as exc:
                LOG.warning("crm_note_failed opportunity=%s error=%s",
                            opportunity_id, type(exc).__name__)
        LOG.info("crm_prospect_synced conversation=%s opportunity=%s created=%s",
                 conversation.id, opportunity_id, result.get("created", True))
    return done


def synced_opportunity_id(db: Session, conversation_id: int) -> int | None:
    """Oportunidade que o SDR já abriu no CRM para esta conversa, se houver."""
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
