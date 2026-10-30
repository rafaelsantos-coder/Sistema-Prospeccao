from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    Activity,
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIKnowledgeItem,
    AIKnowledgeMetadata,
    AIQueueBinding,
    AIRun,
    Conversation,
    ConversationAssignment,
    Lead,
    Message,
    ServiceQueue,
)
from app.services.ai_quick_replies import QuickReply, quick_replies_for_message
from app.services.ai_sdr_policy import CORPORATE_PLANS, FTTH_PLANS, MOBILE_PLANS, SERVICE_CITIES, SULIA_SYSTEM_PROMPT
from app.services.audit import write_audit
from app.services.queue_routing import assign_queue_user, queue_for_conversation
from app.timeutils import is_past, utcnow


LOG = logging.getLogger("sulnet_ai_sdr")


DEFAULT_SYSTEM_PROMPT = SULIA_SYSTEM_PROMPT


class QualificationData(BaseModel):
    current_provider: str | None = None
    current_speed: str | None = None
    current_price: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    desired_product: str | None = None
    customer_type: Literal["RESIDENCIAL", "EMPRESA", "NAO_INFORMADO"] = "NAO_INFORMADO"
    existing_customer: Literal["CLIENTE", "NAO_CLIENTE", "NAO_INFORMADO"] = "NAO_INFORMADO"
    decision_maker: Literal["SIM", "NAO", "NAO_INFORMADO"] = "NAO_INFORMADO"
    timeframe: str | None = None
    city: str | None = None
    address: str | None = None


class AIDecision(BaseModel):
    reply_text: str = Field(min_length=1, max_length=1800)
    quick_replies: list[QuickReply] = Field(default_factory=list, max_length=3)
    intent: Literal[
        "INFORMATION",
        "QUALIFYING",
        "INTEREST",
        "HIGH_INTENT",
        "PROPOSAL_REQUEST",
        "OBJECTION_PRICE",
        "OBJECTION_QUALITY",
        "OBJECTION_SUPPORT",
        "HUMAN_REQUEST",
        "NOT_INTERESTED",
        "DO_NOT_CONTACT",
        "UNKNOWN",
    ] = "UNKNOWN"
    qualification_score: int = Field(ge=0, le=100)
    temperature: Literal["FRIO", "MORNO", "QUENTE"]
    recommended_stage: Literal[
        "NOVO",
        "TENTATIVA_CONTATO",
        "CONTATO_REALIZADO",
        "QUALIFICADO",
        "PROPOSTA",
        "NEGOCIACAO",
        "NAO_CONTATAR",
    ]
    handoff_recommended: bool
    handoff_reason: str = Field(default="", max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=1800)
    next_action: str = Field(max_length=600)
    qualification: QualificationData = Field(default_factory=QualificationData)
    crm_notes: list[str] = Field(default_factory=list)
    objection: Literal[
        "NONE",
        "PRICE",
        "NO_INTEREST",
        "CURRENT_PROVIDER",
        "FIDELITY",
        "TIMING",
        "DECISION_MAKER",
        "TRUST",
        "INSTALLATION",
        "MONEY_NOW",
        "OTHER",
    ] = "NONE"
    sales_state: Literal[
        "NEW_LEAD",
        "DISCOVERY",
        "QUALIFIED",
        "OFFERING",
        "NEGOTIATING",
        "WAITING_CUSTOMER",
        "PURCHASE_INTENT",
        "READY_FOR_HUMAN_SALES",
        "OPT_OUT",
    ] = "DISCOVERY"
    offer_plan_code: str | None = None
    offer_price: float | None = None
    concession_level: int = Field(default=0, ge=0, le=3)
    free_months: int = Field(default=0, ge=0, le=3)
    customer_commitment: bool = False
    followup_needed: bool = True
    customer_needs: list[str] = Field(default_factory=list)
    benefits_valued: list[str] = Field(default_factory=list)
    commercial_arguments_used: list[str] = Field(default_factory=list)
    proposal_summary: str = Field(default="", max_length=800)
    unanswered_question: str | None = Field(default=None, max_length=500)
    journey_state: Literal[
        "IDENTIFY_CUSTOMER",
        "CHOOSE_PRODUCT",
        "CHOOSE_SEGMENT",
        "CONFIRM_CITY",
        "SELECT_OFFER",
        "HANDLE_OBJECTION",
        "CONFIRM_PURCHASE",
        "CLOSING",
        "HUMAN_HANDOFF",
        "OPT_OUT",
        "DISCOVERY",
    ] = "DISCOVERY"
    journey_pending_action: str = Field(default="", max_length=600)
    handoff_target: Literal["NONE", "CLOSER_AI", "HUMAN"] = "NONE"
    stagnation_count: int = Field(default=0, ge=0, le=20)


class AIConfigurationError(RuntimeError):
    pass


def queue_ai_binding(db: Session, queue_id: int | None) -> AIQueueBinding | None:
    if not queue_id:
        return None
    return db.scalar(
        select(AIQueueBinding)
        .options(
            selectinload(AIQueueBinding.agent),
            selectinload(AIQueueBinding.queue),
        )
        .where(
            AIQueueBinding.queue_id == queue_id,
            AIQueueBinding.active.is_(True),
        )
    )


def binding_for_conversation(db: Session, conversation_id: int) -> AIQueueBinding | None:
    queue = queue_for_conversation(db, conversation_id)
    if not queue:
        return None
    return queue_ai_binding(db, queue.id)


def enqueue_ai_job(
    db: Session,
    *,
    conversation: Conversation,
    message: Message,
) -> AIJob | None:
    if not settings.ai_sdr_enabled and not settings.ai_seller_enabled:
        return None
    if message.direction != "INBOUND" or message.message_type != "TEXT":
        return None

    existing = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
    if existing:
        return existing

    # Depois do handoff, a conversa pertence ao Vendedor IA e não volta ao SDR.
    closing_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id,
            AIConversationState.state == "CLOSING",
        )
    )
    if closing_state and settings.ai_seller_enabled:
        closer = db.get(AIAgentProfile, closing_state.agent_id)
        queue = queue_for_conversation(db, conversation.id)
        if closer and closer.active and closer.role_type == "CLOSER" and queue:
            from app.services.ai_followups import cancel_pending_followups
            cancel_pending_followups(db, conversation.id, reason="CLIENTE_RESPONDEU_CLOSER")
            job = AIJob(
                agent_id=closer.id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                message_id=message.id,
                status="PENDENTE",
                available_at=utcnow(),
            )
            db.add(job)
            db.flush()
            LOG.info(
                "ai_closer_job_enqueued job=%s conversation=%s message=%s queue=%s agent=%s",
                job.id,
                conversation.id,
                message.id,
                queue.id,
                closer.id,
            )
            return job

    if not settings.ai_sdr_enabled:
        return None

    binding = binding_for_conversation(db, conversation.id)
    if not binding:
        queue = queue_for_conversation(db, conversation.id)
        LOG.info(
            "ai_job_skipped conversation=%s reason=no_binding queue=%s team=%s",
            conversation.id,
            queue.id if queue else None,
            queue.team if queue else None,
        )
        return None
    if not binding.agent or not binding.agent.active:
        LOG.info(
            "ai_job_skipped conversation=%s reason=agent_inactive queue=%s",
            conversation.id,
            binding.queue_id,
        )
        return None
    if binding.handling_mode == "HUMAN":
        LOG.info(
            "ai_job_skipped conversation=%s reason=human_mode queue=%s",
            conversation.id,
            binding.queue_id,
        )
        return None

    from app.services.ai_followups import cancel_pending_followups
    cancel_pending_followups(
        db,
        conversation.id,
        reason="CLIENTE_RESPONDEU",
    )

    job = AIJob(
        agent_id=binding.agent_id,
        queue_id=binding.queue_id,
        conversation_id=conversation.id,
        message_id=message.id,
        status="PENDENTE",
        available_at=utcnow(),
    )
    db.add(job)
    db.flush()
    LOG.info(
        "ai_job_enqueued job=%s conversation=%s message=%s queue=%s agent=%s",
        job.id,
        conversation.id,
        message.id,
        binding.queue_id,
        binding.agent_id,
    )
    return job


def claim_ai_job(db: Session) -> AIJob | None:
    now = utcnow()
    job = db.scalar(
        select(AIJob)
        .where(
            AIJob.status == "PENDENTE",
            AIJob.available_at <= now,
        )
        .order_by(AIJob.available_at.asc(), AIJob.created_at.asc(), AIJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not job:
        return None
    job.status = "PROCESSANDO"
    job.locked_at = now
    job.attempts = int(job.attempts or 0) + 1
    db.flush()
    return job


def _tokens(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", (value or "").lower())
        if len(item) >= 4
    }


def retrieve_knowledge(
    db: Session,
    *,
    agent: AIAgentProfile,
    query: str,
) -> list[AIKnowledgeItem]:
    items = list(
        db.scalars(
            select(AIKnowledgeItem)
            .where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.active.is_(True),
            )
            .order_by(AIKnowledgeItem.updated_at.desc(), AIKnowledgeItem.id.desc())
            .limit(80)
        )
    )
    if not items:
        return []

    metadata_rows = list(
        db.scalars(
            select(AIKnowledgeMetadata).where(
                AIKnowledgeMetadata.knowledge_item_id.in_([item.id for item in items])
            )
        )
    )
    metadata = {row.knowledge_item_id: row for row in metadata_rows}
    now = utcnow()

    query_tokens = _tokens(query)
    ranked: list[tuple[int, int, int, AIKnowledgeItem]] = []
    for item in items:
        meta = metadata.get(item.id)
        if meta and meta.approval_status != "APPROVED":
            continue
        if meta and is_past(meta.valid_until, now):
            continue
        haystack = (
            f"{item.title} {item.category} {item.tags} {item.content} "
            f"{meta.subject if meta else ''} {meta.product_service if meta else ''} "
            f"{meta.region if meta else ''}"
        ).lower()
        semantic_score = sum(
            4 if token in (item.title or "").lower() else 1
            for token in query_tokens
            if token in haystack
        )
        priority = int(meta.priority if meta else 50)
        ranked.append((semantic_score, priority, item.id, item))

    ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    if query_tokens:
        matched = [row for row in ranked if row[0] > 0]
        pool = matched if matched else ranked
    else:
        pool = ranked

    selected: list[AIKnowledgeItem] = []
    chars = 0
    max_chars = max(2000, int(agent.max_knowledge_chars or 12000))
    for _score, _priority, _item_id, item in pool:
        size = len(item.content or "") + len(item.title or "")
        if selected and chars + size > max_chars:
            break
        selected.append(item)
        chars += size
        if len(selected) >= 10:
            break

    return selected


def _conversation_messages(
    db: Session,
    conversation_id: int,
    *,
    limit: int,
) -> list[Message]:
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction.in_({"INBOUND", "OUTBOUND"}),
            )
            .order_by(Message.id.desc())
            .limit(max(4, min(limit, 80)))
        )
    )
    return list(reversed(rows))


def _confirmed_customer_type_from_lead(lead: Lead | None) -> str:
    if not lead:
        return ""
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    marker = payload.get("ai_customer_type")
    if not isinstance(marker, dict):
        return ""
    if str(marker.get("source") or "").upper() != "INBOUND_CUSTOMER_CONFIRMED":
        return ""
    value = str(marker.get("value") or "").upper()
    return value if value in {"EMPRESA", "RESIDENCIAL"} else ""


def _persist_confirmed_customer_type(
    lead: Lead,
    customer_type: str,
    *,
    conversation_id: int,
) -> None:
    value = str(customer_type or "").upper()
    if value not in {"EMPRESA", "RESIDENCIAL"}:
        return
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["ai_customer_type"] = {
        "value": value,
        "source": "INBOUND_CUSTOMER_CONFIRMED",
        "conversation_id": conversation_id,
        "updated_at": utcnow().isoformat(),
    }
    lead.raw_data = json.dumps(payload, ensure_ascii=False, default=str)
    lead.segment = "Empresa" if value == "EMPRESA" else "Residencial"


def _confirmed_existing_customer_from_lead(lead: Lead | None) -> str:
    if not lead:
        return ""
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    marker = payload.get("ai_existing_customer")
    if not isinstance(marker, dict):
        return ""
    if str(marker.get("source") or "").upper() != "INBOUND_CUSTOMER_CONFIRMED":
        return ""
    value = str(marker.get("value") or "").upper()
    return value if value in {"CLIENTE", "NAO_CLIENTE"} else ""


def _persist_confirmed_existing_customer(
    lead: Lead,
    status: str,
    *,
    conversation_id: int,
) -> None:
    value = str(status or "").upper()
    if value not in {"CLIENTE", "NAO_CLIENTE"}:
        return
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["ai_existing_customer"] = {
        "value": value,
        "source": "INBOUND_CUSTOMER_CONFIRMED",
        "conversation_id": conversation_id,
        "updated_at": utcnow().isoformat(),
    }
    lead.raw_data = json.dumps(payload, ensure_ascii=False, default=str)


def _journey_memory_from_lead(lead: Lead | None) -> dict:
    if not lead:
        return {}
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    journey = payload.get("ai_journey")
    return dict(journey) if isinstance(journey, dict) else {}


def _persist_journey_memory(
    lead: Lead,
    decision: AIDecision,
    *,
    conversation_id: int,
) -> None:
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload["ai_journey"] = {
        "state": decision.journey_state,
        "pending_action": decision.journey_pending_action or decision.next_action,
        "desired_product": decision.qualification.desired_product or "",
        "customer_type": decision.qualification.customer_type,
        "existing_customer": decision.qualification.existing_customer,
        "offer_plan_code": decision.offer_plan_code or "",
        "offer_price": decision.offer_price,
        "objection": decision.objection,
        "handoff_target": decision.handoff_target,
        "stagnation_count": int(decision.stagnation_count or 0),
        "conversation_id": conversation_id,
        "updated_at": utcnow().isoformat(),
    }
    lead.raw_data = json.dumps(payload, ensure_ascii=False, default=str)


def build_ai_context(
    db: Session,
    *,
    job: AIJob,
    agent: AIAgentProfile,
) -> dict:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        raise AIConfigurationError("Conversa do job não existe.")
    message = db.get(Message, job.message_id)
    if not message:
        raise AIConfigurationError("Mensagem do job não existe.")
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    queue = db.get(ServiceQueue, job.queue_id)
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    history = _conversation_messages(
        db,
        conversation.id,
        limit=int(agent.max_history_messages or 24),
    )
    knowledge = retrieve_knowledge(db, agent=agent, query=message.body or "")
    knowledge_meta_rows = list(
        db.scalars(
            select(AIKnowledgeMetadata).where(
                AIKnowledgeMetadata.knowledge_item_id.in_([item.id for item in knowledge])
            )
        )
    ) if knowledge else []
    knowledge_meta = {row.knowledge_item_id: row for row in knowledge_meta_rows}

    return {
        "agent": {
            "name": agent.name,
            "role": agent.role_type,
            "execution_mode": agent.execution_mode,
            "business_goal": agent.business_goal,
            "handoff_score": agent.qualification_handoff_score,
        },
        "queue": {
            "id": queue.id if queue else job.queue_id,
            "name": queue.name if queue else "",
            "team": queue.team if queue else "",
        },
        "lead": {
            "id": lead.id if lead else None,
            "contact_name": lead.contact_name if lead else "",
            "city": lead.city if lead else "",
            "stage": lead.stage if lead else "",
            "score": lead.score if lead else 0,
            "temperature": lead.temperature if lead else "FRIO",
            "confirmed_customer_type": _confirmed_customer_type_from_lead(lead),
            "confirmed_existing_customer": _confirmed_existing_customer_from_lead(lead),
            "journey": _journey_memory_from_lead(lead),
        },
        "previous_ai_state": {
            "qualification_score": state.qualification_score if state else 0,
            "temperature": state.temperature if state else "FRIO",
            "last_intent": state.last_intent if state else "UNKNOWN",
        },
        "customer_evidence": [
            {
                "body": item.body,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in history
            if item.direction == "INBOUND"
        ],
        "history": [
            {
                "direction": item.direction,
                "type": item.message_type,
                "body": item.body,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in history
        ],
        "latest_customer_message": message.body,
        "knowledge": [
            {
                "title": item.title,
                "category": item.category,
                "content": item.content,
                "subject": knowledge_meta[item.id].subject if item.id in knowledge_meta else "",
                "product_service": knowledge_meta[item.id].product_service if item.id in knowledge_meta else "",
                "region": knowledge_meta[item.id].region if item.id in knowledge_meta else "",
                "source": knowledge_meta[item.id].source_reference if item.id in knowledge_meta else item.source_type,
                "priority": knowledge_meta[item.id].priority if item.id in knowledge_meta else 50,
                "valid_until": (
                    knowledge_meta[item.id].valid_until.isoformat()
                    if item.id in knowledge_meta and knowledge_meta[item.id].valid_until
                    else None
                ),
            }
            for item in knowledge
        ],
    }


def _effective_execution_mode(agent: AIAgentProfile) -> str:
    if not settings.ai_autonomous_enabled:
        return "SHADOW"
    return agent.execution_mode


def _fold_sales_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _existing_customer_from_text(
    value: str | None,
    *,
    previous_outbound: str | None = None,
) -> str:
    folded = _fold_sales_text(value)
    previous = _fold_sales_text(previous_outbound)
    if not folded:
        return ""

    negative_exact = {
        "nao sou cliente",
        "ainda nao sou cliente",
        "nunca fui cliente",
    }
    negative_sulnet_cues = (
        "nao sou cliente sulnet",
        "nao sou cliente da sulnet",
        "nao tenho sulnet",
        "nao sou da sulnet",
    )
    positive_exact = {
        "sou cliente",
        "ja sou cliente",
    }
    positive_sulnet_cues = (
        "sou cliente sulnet",
        "sou cliente da sulnet",
        "ja sou cliente sulnet",
        "ja sou cliente da sulnet",
        "ja tenho sulnet",
        "tenho sulnet",
    )
    if folded in negative_exact or any(cue in folded for cue in negative_sulnet_cues):
        return "NAO_CLIENTE"
    if folded in positive_exact or any(cue in folded for cue in positive_sulnet_cues):
        return "CLIENTE"

    status_question = any(
        cue in previous
        for cue in (
            "voce ja e cliente sulnet",
            "ja e cliente sulnet",
            "voce ja e nosso cliente",
        )
    )
    if status_question:
        if folded in {"sim", "sim sou", "sou", "ja sou", "sou sim"}:
            return "CLIENTE"
        if folded in {"nao", "nao sou", "ainda nao", "nao ainda"}:
            return "NAO_CLIENTE"
    return ""


def _existing_customer_from_context(context: dict) -> str:
    previous = _last_outbound_text(context)
    latest = _existing_customer_from_text(
        str(context.get("latest_customer_message") or ""),
        previous_outbound=previous,
    )
    if latest:
        return latest

    confirmed = str(
        (context.get("lead") or {}).get("confirmed_existing_customer") or ""
    ).upper()
    if confirmed in {"CLIENTE", "NAO_CLIENTE"}:
        return confirmed

    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        value = _existing_customer_from_text(str(row.get("body") or ""))
        if value:
            return value
    return ""


def _customer_type_from_text(value: str | None) -> str:
    folded = _fold_sales_text(value)
    if not folded:
        return ""

    company_exact = {
        "empresa",
        "para empresa",
        "plano empresa",
        "plano empresarial",
        "plano para empresa",
        "corporativo",
        "pj",
        "pessoa juridica",
    }
    company_cues = (
        "minha empresa",
        "nossa empresa",
        "meu negocio",
        "meu comercio",
        "minha loja",
        "meu escritorio",
        "quero para empresa",
        "preciso para empresa",
        "internet para empresa",
        "plano corporativo",
        "agora e para empresa",
    )
    residential_exact = {
        "casa",
        "para casa",
        "residencial",
        "plano residencial",
        "internet residencial",
    }
    residential_cues = (
        "minha casa",
        "minha residencia",
        "meu apartamento",
        "quero para casa",
        "preciso para casa",
        "internet para casa",
        "agora e para casa",
    )

    if folded in company_exact or any(cue in folded for cue in company_cues):
        return "EMPRESA"
    if folded in residential_exact or any(cue in folded for cue in residential_cues):
        return "RESIDENCIAL"
    return ""


def _customer_type_from_context(context: dict) -> str:
    latest_type = _customer_type_from_text(
        str(context.get("latest_customer_message") or "")
    )
    if latest_type:
        return latest_type

    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        value = _customer_type_from_text(str(row.get("body") or ""))
        if value:
            return value

    confirmed = str(
        (context.get("lead") or {}).get("confirmed_customer_type") or ""
    ).upper()
    return confirmed if confirmed in {"EMPRESA", "RESIDENCIAL"} else ""


def _customer_inbound_text(context: dict) -> str:
    parts = []
    for row in context.get("history") or []:
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        parts.append(_fold_sales_text(str(row.get("body") or "")))
    return " ".join(part for part in parts if part).strip()


def _numbers_in_text(value: str | None) -> set[str]:
    return set(re.findall(r"\d+", _fold_sales_text(value)))


def _sanitize_unconfirmed_customer_facts(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    inbound = _customer_inbound_text(context)
    qualification = decision.qualification
    confirmed_customer_type = _customer_type_from_context(context)
    qualification.customer_type = (
        confirmed_customer_type if confirmed_customer_type else "NAO_INFORMADO"
    )
    confirmed_existing_customer = _existing_customer_from_context(context)
    qualification.existing_customer = (
        confirmed_existing_customer if confirmed_existing_customer else "NAO_INFORMADO"
    )

    provider = _fold_sales_text(qualification.current_provider)
    if provider and provider not in inbound:
        original_provider = qualification.current_provider or ""
        qualification.current_provider = None
        for field_name in ("reply_text", "summary", "next_action", "proposal_summary"):
            value = getattr(decision, field_name, "") or ""
            if original_provider and original_provider.casefold() in value.casefold():
                value = re.sub(
                    re.escape(original_provider),
                    "seu provedor atual",
                    value,
                    flags=re.IGNORECASE,
                )
                setattr(decision, field_name, value)

    speed_numbers = _numbers_in_text(qualification.current_speed)
    inbound_numbers = _numbers_in_text(inbound)
    if qualification.current_speed and speed_numbers and speed_numbers.isdisjoint(inbound_numbers):
        qualification.current_speed = None

    price_numbers = _numbers_in_text(qualification.current_price)
    if qualification.current_price and price_numbers and price_numbers.isdisjoint(inbound_numbers):
        qualification.current_price = None

    return decision


_SECONDARY_DECLINES = {
    "nao",
    "nao obrigado",
    "nao obrigada",
    "agora nao",
    "prefiro nao",
    "dispenso",
}

_AUXILIARY_QUESTION_CUES = (
    "quer que eu confira",
    "quer que eu verifique",
    "quer que eu explique",
    "quer que eu tambem",
    "quer saber como",
    "posso conferir",
    "posso verificar",
    "posso explicar",
    "como funciona",
    "como acessar",
    "sulnet play",
    "beneficio",
    "beneficios",
    "conteudo",
    "acesso",
)


def _plan_from_sales_context(decision: AIDecision, context: dict):
    code = (decision.offer_plan_code or "").upper()
    if code:
        plan = next(
            (
                item
                for item in FTTH_PLANS
                if code == item["code"] or str(item["download_mbps"]) in code
            ),
            None,
        )
        if plan:
            return plan

    for row in reversed(context.get("history") or []):
        body = str(row.get("body") or "")
        match = re.search(r"\b(250|500|650|800)\s*mega\b", body, re.I)
        if not match:
            continue
        speed = int(match.group(1))
        plan = next(
            (item for item in FTTH_PLANS if item["download_mbps"] == speed),
            None,
        )
        if plan:
            return plan
    return None


def _normalized_city(value: str | None) -> str:
    return _fold_sales_text(value)


def _city_from_sales_context(decision: AIDecision, context: dict) -> str:
    lead_city = str((context.get("lead") or {}).get("city") or "").strip()
    if lead_city:
        return lead_city

    qualification_city = str(decision.qualification.city or "").strip()
    if qualification_city:
        return qualification_city

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    for service_city in SERVICE_CITIES:
        city_key = _normalized_city(service_city)
        if latest == city_key or city_key in latest:
            return service_city
    return ""


def _plan_available_in_city(plan: dict, city: str) -> bool:
    city_key = _normalized_city(city)
    if not city_key:
        return False

    service_city_keys = {_normalized_city(item) for item in SERVICE_CITIES}
    if city_key not in service_city_keys:
        return False

    cities = plan.get("cities")
    if cities == "ALL_20":
        return True
    return city_key in {_normalized_city(str(item)) for item in (cities or [])}


def _eligible_plans_for_city(city: str) -> list[dict]:
    plans = [plan for plan in FTTH_PLANS if _plan_available_in_city(plan, city)]
    return sorted(plans, key=lambda item: int(item["download_mbps"]), reverse=True)


def _requested_plan_speed(value: str | None) -> int | None:
    folded = _fold_sales_text(value)
    if not folded:
        return None

    match = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", folded)
    if not match:
        return None

    speed = int(match.group(1))
    request_cues = (
        "quero",
        "queria",
        "gostaria",
        "interesse",
        "preciso",
        "tem ",
        "voces tem",
        "plano",
        "contratar",
        "oferece",
        "disponivel",
        "trabalham com",
    )
    current_plan_cues = (
        "eu tenho",
        "tenho hoje",
        "uso ",
        "meu plano",
        "minha internet",
        "atualmente",
        "hoje tenho",
    )

    if any(cue in folded for cue in current_plan_cues) and not any(
        cue in folded for cue in request_cues
    ):
        return None

    compact = folded in {
        f"{speed} mega",
        f"{speed} mb",
        f"{speed} mbps",
    }
    if compact or any(cue in folded for cue in request_cues):
        return speed
    return None


def _format_brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _last_outbound_text(context: dict) -> str:
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            return str(row.get("body") or "")
    return ""


def _looks_like_catalog_request(value: str | None) -> bool:
    folded = _fold_sales_text(value)
    if not folded:
        return False
    cues = (
        "todos os planos",
        "todos planos",
        "quais planos",
        "lista de planos",
        "catalogo",
        "planos disponiveis",
        "opcoes de planos",
        "planos da sulnet",
        "planos voces tem",
        "planos que voces tem",
    )
    if any(cue in folded for cue in cues):
        return True
    return (
        "planos" in folded
        and ("precos" in folded or "preco" in folded)
        and ("velocidades" in folded or "velocidade" in folded or "promocoes" in folded)
    )


def _reset_catalog_handoff(decision: AIDecision) -> None:
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True


def _show_catalog_root(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Claro! Tenho o catálogo comercial vigente da Sulnet com os valores confirmados. "
        "O que você quer consultar primeiro?"
    )
    decision.quick_replies = [
        QuickReply(id="catalogo_fibra", label="Fibra óptica"),
        QuickReply(id="catalogo_sulmovel", label="Móvel"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.summary = "Cliente solicitou o catálogo de planos da Sulnet."
    decision.next_action = "Cliente escolher entre Fibra óptica e Sulmóvel."
    _reset_catalog_handoff(decision)
    return decision


def _show_mobile_catalog_page(
    decision: AIDecision,
    *,
    page: int,
) -> AIDecision:
    decision.qualification.desired_product = "Sulmóvel"
    if page <= 1:
        plans = MOBILE_PLANS[:2]
        decision.reply_text = (
            "No Sulmóvel temos planos nacionais com valores confirmados. "
            "Escolha uma opção para continuar:"
        )
        decision.quick_replies = [
            QuickReply(
                id=f"mobile_{int(plan['data_gb'])}gb",
                label=f"{int(plan['data_gb'])}GB - R$ {_format_brl(float(plan['price']))}",
            )
            for plan in plans
        ] + [QuickReply(id="mobile_mais", label="Mais planos")]
        decision.next_action = "Cliente escolher 5GB, 7GB ou abrir mais planos móveis."
    else:
        plans = MOBILE_PLANS[2:4]
        decision.reply_text = "Mais opções do Sulmóvel:"
        decision.quick_replies = [
            QuickReply(
                id=f"mobile_{int(plan['data_gb'])}gb",
                label=f"{int(plan['data_gb'])}GB - R$ {_format_brl(float(plan['price']))}",
            )
            for plan in plans
        ] + [QuickReply(id="mobile_voltar", label="Voltar")]
        decision.next_action = "Cliente escolher 10GB, 15GB ou voltar aos primeiros planos."

    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.summary = "Cliente está consultando o catálogo Sulmóvel."
    _reset_catalog_handoff(decision)
    return decision


def _show_fiber_catalog(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    city = _city_from_sales_context(decision, context)
    decision.qualification.desired_product = "Fibra óptica"
    if not city:
        decision.reply_text = (
            "Claro! Para eu mostrar somente os planos de fibra óptica disponíveis, "
            "qual é a sua cidade?"
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.recommended_stage = "CONTATO_REALIZADO"
        decision.summary = "Cliente escolheu consultar os planos de fibra óptica."
        decision.next_action = "Confirmar a cidade para listar os planos de fibra válidos."
        _reset_catalog_handoff(decision)
        return decision

    eligible = _eligible_plans_for_city(city)
    if not eligible:
        return decision

    decision.qualification.city = city
    option_parts = [
        f"{int(plan['download_mbps'])} Mega por R$ {_format_brl(float(plan['price_fidelity']))}"
        for plan in eligible
    ]
    if len(option_parts) == 1:
        options_text = option_parts[0]
    else:
        options_text = ", ".join(option_parts[:-1]) + f" e {option_parts[-1]}"
    decision.reply_text = (
        f"Em {city}, os planos de fibra óptica disponíveis são {options_text} mensais. "
        "Qual você quer conhecer?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"plano_{int(plan['download_mbps'])}",
            label=f"{int(plan['download_mbps'])} Mega",
        )
        for plan in eligible[:3]
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.summary = f"Cliente está consultando os planos de fibra disponíveis em {city}."
    decision.next_action = "Cliente escolher um plano de fibra válido para avançar."
    decision.proposal_summary = (
        f"Planos de fibra válidos em {city}: " + "; ".join(option_parts) + "."
    )
    _reset_catalog_handoff(decision)
    return decision


def _selected_fiber_plan(value: str | None, city: str) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", folded)
    if not match:
        return None
    speed = int(match.group(1))
    return next(
        (
            plan
            for plan in _eligible_plans_for_city(city)
            if int(plan["download_mbps"]) == speed
        ),
        None,
    )


def _selected_mobile_plan(value: str | None) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(5|7|10|15)\s*gb\b", folded)
    if not match:
        return None
    data_gb = int(match.group(1))
    return next(
        (plan for plan in MOBILE_PLANS if int(plan["data_gb"]) == data_gb),
        None,
    )


def _show_selected_fiber_plan(
    decision: AIDecision,
    context: dict,
    plan: dict,
) -> AIDecision:
    city = _city_from_sales_context(decision, context)
    price = float(plan["price_fidelity"])
    speed = int(plan["download_mbps"])
    decision.reply_text = (
        f"Legal! Em {city} temos o plano de fibra óptica de {speed} Mega "
        f"por R$ {_format_brl(price)} mensais. Quer aproveitar e contratar esse plano agora?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_fibra", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.qualification.city = city
    decision.qualification.desired_product = f"Fibra {speed} Mega"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano de fibra de {speed} Mega em {city}."
    decision.next_action = "Confirmar se o cliente quer contratar o plano escolhido."
    decision.proposal_summary = (
        f"Fibra {speed} Mega em {city} por R$ {_format_brl(price)} mensais."
    )
    return decision


def _show_selected_mobile_plan(
    decision: AIDecision,
    plan: dict,
) -> AIDecision:
    data_gb = int(plan["data_gb"])
    price = float(plan["price"])
    apps = ", ".join(plan.get("unlimited_apps") or [])
    details = f" com {apps} ilimitado" if apps else ""
    decision.reply_text = (
        f"Legal! O Sulmóvel {data_gb}GB custa R$ {_format_brl(price)} mensais, "
        f"com {int(plan['minutes'])} minutos e {int(plan['sms'])} SMS{details}. "
        "Quer contratar esse plano?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_movel", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.qualification.desired_product = f"Sulmóvel {data_gb}GB"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano Sulmóvel {data_gb}GB."
    decision.next_action = "Confirmar se o cliente quer contratar o plano móvel escolhido."
    decision.proposal_summary = (
        f"Sulmóvel {data_gb}GB por R$ {_format_brl(price)} mensais."
    )
    return decision


def _looks_like_corporate_differential_question(value: str | None) -> bool:
    folded = _fold_sales_text(value)
    if not folded:
        return False
    cues = (
        "qual a diferenca",
        "qual diferenca",
        "diferencial",
        "vantagem",
        "porque e mais caro",
        "por que e mais caro",
        "pq e mais caro",
        "sla",
        "8 horas",
        "oito horas",
        "suporte nivel 2",
        "suporte n2",
        "nivel 2",
        "n2",
        "banda compartilhada",
        "compartilhada",
        "link dedicado",
        "ip fixo",
        "sulnet monitora",
        "monitora",
        "noc",
        "monitoramento",
        "queda",
        "oscilacao",
        "empresa ficar parada",
        "atendimento prioritario",
        "atendimento empresarial",
    )
    return any(cue in folded for cue in cues)


def _show_corporate_differentials(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "O link corporativo é pensado para manter sua empresa operando: tem SLA de atendimento de até 8 horas, "
        "suporte nível 2 e equipe especializada em empresas. A entrega da banda é diferenciada e não é compartilhada "
        "da mesma forma que no residencial; além disso, os planos têm IP fixo e Sulnet Monitora, com o NOC acompanhando "
        "quedas ou oscilações e atuando de forma proativa sempre que possível. Quer ver as opções corporativas?"
    )
    decision.quick_replies = [
        QuickReply(id="ver_planos_corp", label="Ver planos"),
        QuickReply(id="outra_duvida_corp", label="Outra dúvida"),
    ]
    decision.intent = "INFORMATION"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = "Internet corporativa"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.summary = (
        "Cliente recebeu os diferenciais do link corporativo: SLA de atendimento até 8h, suporte N2, "
        "equipe empresarial, banda diferenciada, IP fixo e monitoramento proativo do NOC."
    )
    decision.next_action = "Oferecer o catálogo corporativo após esclarecer os diferenciais."
    decision.proposal_summary = (
        "Diferenciais corporativos: SLA de atendimento até 8h; suporte N2; equipe especializada; "
        "entrega de banda diferenciada; IP fixo; Sulnet Monitora/NOC."
    )
    return decision


def _show_corporate_catalog(decision: AIDecision) -> AIDecision:
    option_parts = [
        f"{int(plan['download_mbps'])} Mega por R$ {_format_brl(float(plan['price']))}"
        for plan in CORPORATE_PLANS
    ]
    decision.reply_text = (
        "Perfeito. Para empresa temos planos corporativos de fibra óptica com atendimento prioritário: "
        + ", ".join(option_parts[:-1])
        + f" e {option_parts[-1]} mensais. Qual você quer conhecer?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"corp_{int(plan['download_mbps'])}",
            label=f"Corp. {int(plan['download_mbps'])} Mega",
        )
        for plan in CORPORATE_PLANS
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = "Internet corporativa"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.summary = "Cliente confirmou atendimento para empresa e recebeu o catálogo corporativo."
    decision.next_action = "Cliente escolher entre Corp. 200, Corp. 250 e Corp. 650."
    decision.proposal_summary = "Planos corporativos: " + "; ".join(option_parts) + "."
    return decision


def _selected_corporate_plan(value: str | None) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(200|250|650)\s*(?:mega|mb|mbps)?\b", folded)
    if not match:
        return None
    speed = int(match.group(1))
    return next(
        (
            plan
            for plan in CORPORATE_PLANS
            if int(plan["download_mbps"]) == speed
        ),
        None,
    )


def _show_selected_corporate_plan(
    decision: AIDecision,
    plan: dict,
) -> AIDecision:
    speed = int(plan["download_mbps"])
    upload = int(plan["upload_mbps"])
    price = float(plan["price"])
    benefits = ", ".join(plan.get("benefits") or [])
    decision.reply_text = (
        f"O Corp. {speed} Mega custa R$ {_format_brl(price)} mensais, com {speed}/{upload} Mbps, "
        f"{benefits} e atendimento prioritário. Quer contratar esse plano?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_corp", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = f"Corporativo {speed} Mega"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.concession_level = 0
    decision.free_months = 0
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano corporativo de {speed} Mega."
    decision.next_action = "Confirmar se o cliente quer contratar o plano corporativo escolhido."
    decision.proposal_summary = (
        f"Corp. {speed} Mega por R$ {_format_brl(price)} mensais; {speed}/{upload} Mbps; {benefits}."
    )
    return decision


def _apply_corporate_customer_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    customer_type = _customer_type_from_context(context)
    if customer_type == "RESIDENCIAL":
        decision.qualification.customer_type = "RESIDENCIAL"
        return decision
    if customer_type != "EMPRESA":
        return decision

    decision.qualification.customer_type = "EMPRESA"
    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _fold_sales_text(_last_outbound_text(context))

    if _looks_like_corporate_differential_question(latest_raw):
        return _show_corporate_differentials(decision)

    if latest in {"ver planos", "ver planos corporativos", "planos corporativos"}:
        return _show_corporate_catalog(decision)

    if _customer_type_from_text(latest_raw) == "EMPRESA":
        return _show_corporate_catalog(decision)

    if _looks_like_catalog_request(latest_raw):
        return _show_corporate_catalog(decision)

    if latest == "ver outros planos" and (
        "corp " in previous
        or "corporativo" in previous
        or "empresa temos planos" in previous
    ):
        return _show_corporate_catalog(decision)

    selected = _selected_corporate_plan(latest_raw)
    if selected and (
        "corp" in latest
        or "corporativo" in previous
        or "empresa temos planos" in previous
        or str((context.get("lead") or {}).get("confirmed_customer_type") or "").upper() == "EMPRESA"
    ):
        return _show_selected_corporate_plan(decision, selected)

    offer_code = str(decision.offer_plan_code or "").upper()
    residential_offer = offer_code.startswith("FTTH_")
    residential_reply = any(
        cue in _fold_sales_text(decision.reply_text)
        for cue in (
            "800 mega por r 119 90",
            "650 mega por r 99 90",
            "250 mega por r 89 90",
            "500 mega por r 119 90",
        )
    )
    if residential_offer or residential_reply:
        return _show_corporate_catalog(decision)

    concession_in_reply = any(
        cue in _fold_sales_text(decision.reply_text)
        for cue in (
            "mes gratis",
            "meses gratis",
            "mensalidade gratis",
            "mensalidades gratis",
        )
    )
    if decision.free_months > 0 or decision.concession_level > 0 or concession_in_reply:
        decision = _show_corporate_catalog(decision)
        decision.reply_text = (
            "Para os planos corporativos, não tenho desconto ou mensalidade grátis confirmados na base vigente. "
            "Posso te apresentar as opções oficiais: 200 Mega por R$ 139,00, 250 Mega por R$ 169,00 "
            "ou 650 Mega por R$ 199,90 mensais. Qual você quer conhecer?"
        )
        return decision

    decision.concession_level = 0
    decision.free_months = 0
    return decision


def _reset_entry_offer(decision: AIDecision) -> None:
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True


def _show_existing_customer_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = "Antes de te mostrar as opções, você já é cliente Sulnet?"
    decision.quick_replies = [
        QuickReply(id="cliente_sulnet_sim", label="Sim"),
        QuickReply(id="cliente_sulnet_nao", label="Não"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "NAO_INFORMADO"
    decision.summary = "SulIA iniciou a qualificação perguntando se a pessoa já é cliente Sulnet."
    decision.next_action = "Confirmar se já é cliente Sulnet."
    _reset_entry_offer(decision)
    return decision


def _show_new_customer_product_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Perfeito! O que você quer conhecer: planos de internet ou Telefonia Móvel?"
    )
    decision.quick_replies = [
        QuickReply(id="prospeccao_internet", label="Internet"),
        QuickReply(id="prospeccao_movel", label="Telefonia Móvel"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "NAO_CLIENTE"
    decision.qualification.desired_product = None
    decision.summary = "Pessoa confirmou que ainda não é cliente Sulnet."
    decision.next_action = "Cliente escolher entre Internet e Telefonia Móvel."
    _reset_entry_offer(decision)
    return decision


def _show_existing_customer_product_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Legal! Para uma nova contratação, você quer Telefonia Móvel ou uma nova conexão "
        "de internet para outro endereço?"
    )
    decision.quick_replies = [
        QuickReply(id="cliente_movel", label="Telefonia Móvel"),
        QuickReply(id="cliente_nova_conexao", label="Nova conexão"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "CLIENTE"
    decision.qualification.desired_product = None
    decision.summary = "Pessoa confirmou que já é cliente Sulnet e busca uma nova contratação."
    decision.next_action = "Cliente escolher Telefonia Móvel ou nova conexão para outro endereço."
    _reset_entry_offer(decision)
    return decision


def _show_internet_segment_question(
    decision: AIDecision,
    *,
    existing_customer: bool,
) -> AIDecision:
    decision.reply_text = (
        "Essa nova conexão para outro endereço é para sua casa ou empresa?"
        if existing_customer
        else "A internet é para sua casa ou empresa?"
    )
    decision.quick_replies = [
        QuickReply(id="internet_casa", label="Para casa"),
        QuickReply(id="internet_empresa", label="Para empresa"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = (
        "CLIENTE" if existing_customer else "NAO_CLIENTE"
    )
    decision.qualification.desired_product = (
        "Nova conexão de internet" if existing_customer else "Internet"
    )
    decision.summary = (
        "Cliente Sulnet escolheu contratar uma nova conexão para outro endereço."
        if existing_customer
        else "Novo cliente escolheu conhecer planos de internet."
    )
    decision.next_action = "Confirmar se a conexão é residencial ou empresarial."
    _reset_entry_offer(decision)
    return decision


def _apply_prospecting_entry_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if decision.intent in {"DO_NOT_CONTACT", "HUMAN_REQUEST"} or decision.sales_state == "OPT_OUT":
        return decision

    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _last_outbound_text(context)
    current_confirmation = _existing_customer_from_text(
        latest_raw,
        previous_outbound=previous,
    )
    status = _existing_customer_from_context(context)

    if not status:
        return _show_existing_customer_question(decision)

    decision.qualification.existing_customer = status

    if current_confirmation == "CLIENTE":
        return _show_existing_customer_product_question(decision)
    if current_confirmation == "NAO_CLIENTE":
        return _show_new_customer_product_question(decision)

    mobile_choices = {
        "movel",
        "telefonia movel",
        "plano movel",
        "planos moveis",
        "sulmovel",
        "celular",
    }
    internet_choices = {
        "internet",
        "planos de internet",
        "plano de internet",
        "fibra",
        "fibra optica",
    }
    new_connection_choices = {
        "nova conexao",
        "outra conexao",
        "nova internet",
        "novo endereco",
        "outro endereco",
        "internet para outro endereco",
    }

    if status == "CLIENTE":
        if latest in mobile_choices:
            result = _show_mobile_catalog_page(decision, page=1)
            result.qualification.existing_customer = "CLIENTE"
            return result
        if latest in new_connection_choices or latest in internet_choices:
            return _show_internet_segment_question(
                decision,
                existing_customer=True,
            )
        return decision

    if latest in mobile_choices:
        result = _show_mobile_catalog_page(decision, page=1)
        result.qualification.existing_customer = "NAO_CLIENTE"
        return result

    if latest in internet_choices:
        customer_type = _customer_type_from_context(context)
        if customer_type == "EMPRESA":
            result = _show_corporate_catalog(decision)
            result.qualification.existing_customer = "NAO_CLIENTE"
            return result
        if customer_type == "RESIDENCIAL":
            result = _show_fiber_catalog(decision, context)
            result.qualification.existing_customer = "NAO_CLIENTE"
            return result
        return _show_internet_segment_question(
            decision,
            existing_customer=False,
        )

    return decision


def _apply_catalog_navigation_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _fold_sales_text(_last_outbound_text(context))

    if _looks_like_catalog_request(latest_raw):
        return _show_catalog_root(decision)

    if latest in {
        "fibra",
        "fibra optica",
        "planos de fibra",
        "internet fibra",
        "internet de fibra",
    }:
        return _show_fiber_catalog(decision, context)

    if latest in {
        "movel",
        "sulmovel",
        "planos moveis",
        "plano movel",
        "celular",
    }:
        return _show_mobile_catalog_page(decision, page=1)

    if latest in {"mais planos", "ver mais", "mais opcoes"}:
        return _show_mobile_catalog_page(decision, page=2)

    if latest == "voltar" and "sulmovel" in previous:
        return _show_mobile_catalog_page(decision, page=1)

    if latest == "ver outros planos":
        if "sulmovel" in previous:
            return _show_mobile_catalog_page(decision, page=1)
        if "fibra optica" in previous:
            return _show_fiber_catalog(decision, context)

    if (
        "qual e a sua cidade" in previous
        and "planos de fibra" in previous
        and _city_from_sales_context(decision, context)
    ):
        return _show_fiber_catalog(decision, context)

    mobile_plan = _selected_mobile_plan(latest_raw)
    if mobile_plan:
        mobile_gb = int(mobile_plan["data_gb"])
        mobile_compact = latest in {f"{mobile_gb}gb", f"{mobile_gb} gb"}
        mobile_button_label = (
            latest.startswith(f"{mobile_gb}gb r ")
            or latest.startswith(f"{mobile_gb} gb r ")
        )
        if "sulmovel" in previous or mobile_compact or mobile_button_label:
            return _show_selected_mobile_plan(decision, mobile_plan)

    city = _city_from_sales_context(decision, context)
    if city:
        fiber_plan = _selected_fiber_plan(latest_raw, city)
        if fiber_plan and (
            "planos de fibra" in previous
            or "fibra optica" in previous
            or latest in {
                f"{int(fiber_plan['download_mbps'])} mega",
                f"{int(fiber_plan['download_mbps'])} mb",
                f"{int(fiber_plan['download_mbps'])} mbps",
            }
        ):
            return _show_selected_fiber_plan(decision, context, fiber_plan)

    return decision


def _apply_unavailable_plan_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    requested_speed = _requested_plan_speed(
        str(context.get("latest_customer_message") or "")
    )
    if requested_speed is None:
        return decision

    city = _city_from_sales_context(decision, context)
    if not city:
        decision.reply_text = (
            f"A disponibilidade do plano de {requested_speed} Mega varia por cidade. "
            "Qual é a sua cidade? Aí te passo as opções corretas e seguimos."
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.customer_commitment = False
        decision.offer_plan_code = None
        decision.offer_price = None
        decision.followup_needed = True
        decision.next_action = "Confirmar a cidade para apresentar somente planos válidos."
        return decision

    eligible = _eligible_plans_for_city(city)
    if not eligible:
        return decision

    if any(
        int(plan["download_mbps"]) == requested_speed
        for plan in eligible
    ):
        return decision

    option_parts = [
        f"{plan['download_mbps']} Mega por R$ {_format_brl(float(plan['price_fidelity']))}"
        for plan in eligible
    ]
    if len(option_parts) == 1:
        options_text = option_parts[0]
    else:
        options_text = ", ".join(option_parts[:-1]) + f" ou {option_parts[-1]}"

    decision.reply_text = (
        f"O plano de {requested_speed} Mega não está disponível em {city}. "
        f"Hoje temos {options_text} mensais. Qual opção você prefere?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"plano_{int(plan['download_mbps'])}",
            label=f"{int(plan['download_mbps'])} Mega",
        )
        for plan in eligible[:3]
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.next_action = "Cliente escolher uma das opções comerciais válidas para a cidade."
    decision.proposal_summary = (
        f"Alternativas válidas em {city}: " + "; ".join(option_parts) + "."
    )
    return decision


def _apply_router_information_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    router_cues = (
        "roteador",
        "roteadores",
        "wifi",
        "wi fi",
        "segundo roteador",
        "outro roteador",
        "mais um roteador",
    )
    if not any(cue in latest for cue in router_cues):
        return decision

    explicit_speed = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", latest)
    if explicit_speed and int(explicit_speed.group(1)) not in {650, 800}:
        return decision

    plan = _plan_from_sales_context(decision, context)
    if not plan:
        speed_match = re.search(r"\b(650|800)\s*mega\b", latest)
        if speed_match:
            speed = int(speed_match.group(1))
            plan = next(
                (item for item in FTTH_PLANS if int(item["download_mbps"]) == speed),
                None,
            )
    if not plan:
        return decision

    speed = int(plan["download_mbps"])
    if speed not in {650, 800}:
        return decision

    included = int(plan.get("included_router_count") or 1)
    additional_price = float(plan.get("additional_router_monthly_price") or 29.90)
    plan_price = float(plan["price_fidelity"])
    asks_fiber = any(
        cue in latest
        for cue in (
            "e fibra",
            "é fibra",
            "internet e fibra",
            "internet é fibra",
            "fibra optica",
            "fibra óptica",
        )
    )

    if asks_fiber:
        opening = (
            f"Sim, o plano de {speed} Mega é de fibra óptica e inclui "
            f"{included} roteador em comodato."
        )
    else:
        opening = (
            f"O plano de {speed} Mega inclui {included} roteador em comodato."
        )

    decision.reply_text = (
        f"{opening} Se precisar de outro, cada roteador adicional é locado por "
        f"R$ {_format_brl(additional_price)} por mês. "
        f"Quer seguir com o plano de {speed} Mega por R$ {_format_brl(plan_price)} mensais?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="tenho_duvida", label="Tenho outra dúvida"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = plan_price
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.objection = "NONE"
    decision.unanswered_question = None
    decision.summary = (
        f"Cliente perguntou sobre roteadores do plano de {speed} Mega; "
        f"informado {included} incluso e adicional por R$ {_format_brl(additional_price)}/mês."
    )
    decision.next_action = "Retomar o fechamento do plano após esclarecer os roteadores."
    decision.proposal_summary = (
        f"Fibra {speed} Mega por R$ {_format_brl(plan_price)} mensais; "
        f"{included} roteador incluso; adicional por R$ {_format_brl(additional_price)}/mês."
    )
    return decision


def _apply_secondary_decline_reengagement_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if latest not in _SECONDARY_DECLINES:
        return decision

    previous_outbound = None
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            previous_outbound = str(row.get("body") or "")
            break
    if not previous_outbound:
        return decision

    previous_folded = _fold_sales_text(previous_outbound)
    # Se a pergunta anterior era diretamente sobre contratar/fechar, a recusa
    # pode ser da oferta principal e não deve ser reinterpretada.
    if "contratar" in previous_folded or "fechar" in previous_folded:
        return decision

    is_auxiliary = any(cue in previous_folded for cue in _AUXILIARY_QUESTION_CUES)
    if not is_auxiliary:
        return decision

    plan = _plan_from_sales_context(decision, context)
    if not plan:
        return decision

    price = float(plan["price_fidelity"])
    price_text = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    decision.reply_text = (
        "Sem problema! Podemos dar andamento à contratação do plano de fibra óptica de "
        f"{plan['download_mbps']} Mega por R$ {price_text} mensais?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="outra_duvida", label="Tenho outra dúvida"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.objection = "NONE"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.followup_needed = True
    decision.next_action = "Retomar o fechamento do plano já apresentado."
    return decision


_OFFER_REJECTION_CUES = (
    "nao quero nada de internet",
    "nao quero internet",
    "nao tenho interesse em internet",
    "nao quero esse plano",
    "nao quero o plano",
    "nao quero contratar",
    "nao tenho interesse",
    "nao quero nada",
)

_EXPLICIT_CONTACT_STOP_CUES = (
    "nao quero mais mensagens",
    "nao quero mais contato",
    "nao me contate",
    "nao entre em contato",
    "nao me chame",
    "pare de me chamar",
    "pare de mandar",
    "me remova",
    "remover meu numero",
    "sair da lista",
    "descadastrar",
)


def _is_company_context(context: dict) -> bool:
    if _customer_type_from_context(context) == "EMPRESA":
        return True
    company_terms = (
        "minha empresa",
        "nossa empresa",
        "para empresa",
        "empresa",
        "cnpj",
        "meu negocio",
        "meu comercio",
        "minha loja",
        "meu escritorio",
    )
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if any(term in body for term in company_terms):
            return True
    return False


def _is_residential_context(context: dict) -> bool:
    if _customer_type_from_context(context) == "RESIDENCIAL":
        return True
    residential_terms = (
        "minha casa",
        "para casa",
        "em casa",
        "residencial",
        "minha residencia",
        "meu apartamento",
    )
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if any(term in body for term in residential_terms):
            return True
    return False


def _apply_offer_rejection_diagnostic_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if not latest:
        return decision
    if any(cue in latest for cue in _EXPLICIT_CONTACT_STOP_CUES):
        return decision
    if not any(cue in latest for cue in _OFFER_REJECTION_CUES):
        return decision

    # Não repetir esse mesmo gatilho indefinidamente.
    for row in context.get("history") or []:
        if str(row.get("direction") or "").upper() != "OUTBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if (
            "possui conexao de internet na sua casa" in body
            or "sua empresa ja possui conexao de internet" in body
        ):
            return decision

    if _is_company_context(context):
        question = "Entendi. Hoje sua empresa já possui conexão de internet?"
    elif _is_residential_context(context):
        question = "Entendi. Hoje você já possui conexão de internet na sua casa?"
    else:
        question = "Entendi. Hoje você já possui conexão de internet?"

    decision.reply_text = question
    decision.quick_replies = [
        QuickReply(id="possui_internet_sim", label="Sim"),
        QuickReply(id="possui_internet_nao", label="Não"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.objection = "NO_INTEREST"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.followup_needed = True
    decision.next_action = (
        "Descobrir a situação atual da conexão antes de retomar uma oferta."
    )
    return decision


_JOURNEY_PENDING_ACTIONS = {
    "IDENTIFY_CUSTOMER": "Confirmar se a pessoa já é cliente Sulnet.",
    "CHOOSE_PRODUCT": "Confirmar se deseja Internet, Telefonia Móvel ou nova conexão, conforme o perfil.",
    "CHOOSE_SEGMENT": "Confirmar se a conexão de internet é para casa ou empresa.",
    "CONFIRM_CITY": "Confirmar a cidade para validar os planos de internet disponíveis.",
    "SELECT_OFFER": "Apresentar opções válidas e obter a escolha de um plano.",
    "HANDLE_OBJECTION": "Resolver a objeção atual com um ângulo novo e avançar a decisão.",
    "CONFIRM_PURCHASE": "Obter aceite explícito para contratar a oferta escolhida.",
    "CLOSING": "Iniciar o Vendedor IA de Fechamento com todo o contexto coletado.",
    "HUMAN_HANDOFF": "Transferir para atendimento humano porque o cliente pediu uma pessoa.",
    "OPT_OUT": "Encerrar contato e cancelar follow-ups.",
    "DISCOVERY": "Descobrir a próxima informação comercial necessária sem repetir perguntas.",
}


def _journey_memory_from_context(context: dict) -> dict:
    value = (context.get("lead") or {}).get("journey")
    return dict(value) if isinstance(value, dict) else {}


def _explicit_human_request(context: dict) -> bool:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    cues = (
        "quero falar com atendente",
        "quero falar com um atendente",
        "falar com atendente",
        "falar com uma pessoa",
        "falar com pessoa",
        "quero falar com uma pessoa",
        "quero falar com humano",
        "atendimento humano",
        "falar com vendedor",
        "quero um vendedor",
        "quero falar com vendedor",
    )
    return any(cue in latest for cue in cues)


def _explicit_purchase_commitment(context: dict) -> bool:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    cues = (
        "quero contratar",
        "pode fechar",
        "pode fazer",
        "vamos fechar",
        "vamos fazer",
        "quero esse plano",
        "vou contratar",
        "pode contratar",
        "com essa condicao eu fecho",
        "fechado",
        "pode dar andamento",
        "vamos dar andamento",
        "pode seguir",
        "pode prosseguir",
        "eu aceito",
        "aceito",
    )
    if any(cue in latest for cue in cues):
        return True

    previous = _fold_sales_text(_last_outbound_text(context))
    purchase_question = any(
        cue in previous
        for cue in (
            "quer contratar",
            "contratar esse plano",
            "quer seguir com o plano",
            "quer aproveitar e contratar",
            "contratar agora",
        )
    )
    return purchase_question and latest in {
        "sim",
        "sim quero",
        "quero",
        "pode",
        "vamos",
        "confirmo",
    }


def _journey_product_kind(decision: AIDecision, context: dict) -> str:
    memory = _journey_memory_from_context(context)
    combined = _fold_sales_text(
        " ".join(
            [
                str(decision.qualification.desired_product or ""),
                str(decision.offer_plan_code or ""),
                str(memory.get("desired_product") or ""),
                str(memory.get("offer_plan_code") or ""),
            ]
        )
    )
    if any(token in combined for token in ("sulmovel", "telefonia movel", "mobile", "movel")):
        return "MOBILE"
    if any(token in combined for token in ("internet", "fibra", "ftth", "corp", "corporativo", "conexao")):
        return "INTERNET"
    return ""


def _derive_journey_state(decision: AIDecision, context: dict) -> str:
    if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
        return "OPT_OUT"
    if _explicit_human_request(context):
        return "HUMAN_HANDOFF"
    if _explicit_purchase_commitment(context):
        return "CLOSING"

    existing_customer = _existing_customer_from_context(context)
    if existing_customer not in {"CLIENTE", "NAO_CLIENTE"}:
        return "IDENTIFY_CUSTOMER"

    product_kind = _journey_product_kind(decision, context)
    if not product_kind:
        return "CHOOSE_PRODUCT"

    if product_kind == "MOBILE":
        if decision.offer_plan_code:
            return "HANDLE_OBJECTION" if decision.objection != "NONE" else "CONFIRM_PURCHASE"
        return "SELECT_OFFER"

    customer_type = _customer_type_from_context(context)
    if customer_type not in {"EMPRESA", "RESIDENCIAL"}:
        return "CHOOSE_SEGMENT"

    city = _city_from_sales_context(decision, context)
    if customer_type == "RESIDENCIAL" and not city:
        return "CONFIRM_CITY"

    if not decision.offer_plan_code:
        return "SELECT_OFFER"
    if decision.objection != "NONE":
        return "HANDLE_OBJECTION"
    return "CONFIRM_PURCHASE"


def _normalize_sdr_reply(value: str | None) -> str:
    return _fold_sales_text(value)


def _recent_sdr_outbound(context: dict, limit: int = 8) -> list[str]:
    values = [
        str(row.get("body") or "").strip()
        for row in context.get("history") or []
        if str(row.get("direction") or "").upper() == "OUTBOUND"
        and str(row.get("body") or "").strip()
    ]
    return values[-max(1, limit):]


def _sdr_reply_is_repetitive(candidate: str, previous_messages: list[str]) -> bool:
    normalized = _normalize_sdr_reply(candidate)
    if not normalized:
        return True
    candidate_tokens = set(normalized.split())
    for previous in previous_messages:
        other = _normalize_sdr_reply(previous)
        if not other:
            continue
        if normalized == other:
            return True
        if SequenceMatcher(None, normalized, other).ratio() >= 0.80:
            return True
        other_tokens = set(other.split())
        if candidate_tokens and other_tokens:
            containment = len(candidate_tokens & other_tokens) / min(
                len(candidate_tokens),
                len(other_tokens),
            )
            if containment >= 0.76:
                return True
    return False


def _apply_stagnation_strategy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    state = decision.journey_state
    existing = _existing_customer_from_context(context)
    customer_type = _customer_type_from_context(context)
    previous_outbound = _recent_sdr_outbound(context)

    if state == "IDENTIFY_CUSTOMER":
        if previous_outbound:
            decision = _show_existing_customer_question(decision)
            decision.reply_text = "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"
            return decision
        return _show_existing_customer_question(decision)
    if state == "CHOOSE_PRODUCT":
        if existing == "CLIENTE":
            decision = _show_existing_customer_product_question(decision)
            if previous_outbound:
                decision.reply_text = "Para sua próxima contratação, prefere Telefonia Móvel ou internet em outro endereço?"
            return decision
        if existing == "NAO_CLIENTE":
            decision = _show_new_customer_product_question(decision)
            if previous_outbound:
                decision.reply_text = "Qual caminho você quer seguir primeiro: Internet ou Telefonia Móvel?"
            return decision
    if state == "CHOOSE_SEGMENT":
        decision = _show_internet_segment_question(
            decision,
            existing_customer=existing == "CLIENTE",
        )
        if previous_outbound:
            decision.reply_text = (
                "E essa nova conexão será residencial ou empresarial?"
                if existing == "CLIENTE"
                else "Essa conexão será residencial ou empresarial?"
            )
        return decision
    if state == "CONFIRM_CITY":
        decision.reply_text = (
            "Em qual cidade será instalada a conexão?"
            if previous_outbound
            else "Para eu te mostrar somente os planos disponíveis, qual é a sua cidade?"
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision
    if state == "SELECT_OFFER":
        if customer_type == "EMPRESA":
            decision = _show_corporate_catalog(decision)
            if previous_outbound:
                decision.reply_text = "Qual das opções corporativas você prefere avaliar agora?"
            return decision
        if _journey_product_kind(decision, context) == "MOBILE":
            decision = _show_mobile_catalog_page(decision, page=1)
            if previous_outbound:
                decision.reply_text = "Qual plano de Telefonia Móvel você quer avaliar agora?"
            return decision
        city = _city_from_sales_context(decision, context)
        if city:
            decision = _show_fiber_catalog(decision, context)
            if previous_outbound:
                decision.reply_text = f"Entre as opções disponíveis em {city}, qual velocidade você quer avaliar?"
            return decision
    if state == "HANDLE_OBJECTION":
        if decision.objection == "PRICE":
            decision.reply_text = "Para eu comparar de forma justa, quanto você paga hoje pela sua conexão?"
            decision.quick_replies = []
        else:
            decision.reply_text = "Para eu te ajudar por outro ângulo, o que mais pesa na sua decisão hoje?"
            decision.quick_replies = [
                QuickReply(id="prioridade_preco", label="Preço"),
                QuickReply(id="prioridade_qualidade", label="Qualidade"),
                QuickReply(id="prioridade_momento", label="Momento"),
            ]
        decision.intent = "QUALIFYING"
        decision.sales_state = "NEGOTIATING"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision
    if state == "CONFIRM_PURCHASE":
        decision.reply_text = "Para seguirmos daqui, você prefere contratar agora ou ver outra opção?"
        decision.quick_replies = [
            QuickReply(id="contratar_agora", label="Contratar agora"),
            QuickReply(id="ver_outra_opcao", label="Ver outra opção"),
        ]
        decision.intent = "INTEREST"
        decision.sales_state = "OFFERING"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision
    return decision


def _apply_journey_orchestration_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    state = _derive_journey_state(decision, context)
    decision.journey_state = state
    decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS.get(
        state,
        _JOURNEY_PENDING_ACTIONS["DISCOVERY"],
    )

    if state == "OPT_OUT":
        decision.handoff_target = "NONE"
        decision.handoff_recommended = False
        decision.followup_needed = False
    elif state == "HUMAN_HANDOFF":
        decision.handoff_target = "HUMAN"
        decision.handoff_recommended = True
        decision.intent = "HUMAN_REQUEST"
        decision.sales_state = "READY_FOR_HUMAN_SALES"
        decision.handoff_reason = (
            decision.handoff_reason
            or "Cliente pediu explicitamente atendimento humano."
        )
        decision.followup_needed = False
    elif state == "CLOSING":
        decision.handoff_target = "CLOSER_AI"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.customer_commitment = True
        decision.sales_state = "PURCHASE_INTENT"
        if decision.intent in {"HUMAN_REQUEST", "UNKNOWN"}:
            decision.intent = "HIGH_INTENT"
        decision.next_action = _JOURNEY_PENDING_ACTIONS["CLOSING"]
        decision.followup_needed = False
    else:
        decision.handoff_target = "NONE"
        # O modelo não pode transformar intenção de compra/qualificação normal
        # em handoff humano ou fechamento sem evidência explícita do cliente.
        if decision.intent != "DO_NOT_CONTACT":
            decision.handoff_recommended = False
            decision.handoff_reason = ""
        if decision.intent == "HUMAN_REQUEST":
            decision.intent = (
                "QUALIFYING"
                if state in {"IDENTIFY_CUSTOMER", "CHOOSE_PRODUCT", "CHOOSE_SEGMENT", "CONFIRM_CITY"}
                else "INTEREST"
            )
        decision.customer_commitment = False
        if decision.sales_state in {"READY_FOR_HUMAN_SALES", "PURCHASE_INTENT"}:
            decision.sales_state = "NEGOTIATING" if decision.objection != "NONE" else "OFFERING"

    mandatory_gate_states = {
        "IDENTIFY_CUSTOMER",
        "CHOOSE_PRODUCT",
        "CHOOSE_SEGMENT",
        "CONFIRM_CITY",
    }
    if state in mandatory_gate_states:
        decision = _apply_stagnation_strategy(decision, context)
        decision.journey_state = state
        decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS[state]
        decision.handoff_target = "NONE"
        decision.handoff_recommended = False
        decision.handoff_reason = ""

    previous = _journey_memory_from_context(context)
    previous_state = str(previous.get("state") or "")
    previous_pending = str(previous.get("pending_action") or "")
    previous_count = int(previous.get("stagnation_count") or 0)
    same_position = (
        previous_state == state
        and previous_pending == decision.journey_pending_action
        and state not in {"CLOSING", "HUMAN_HANDOFF", "OPT_OUT"}
    )
    decision.stagnation_count = min(20, previous_count + 1) if same_position else 0

    repetitive = _sdr_reply_is_repetitive(
        decision.reply_text,
        _recent_sdr_outbound(context),
    )
    if repetitive or decision.stagnation_count >= 3:
        decision = _apply_stagnation_strategy(decision, context)
        # As funções de fallback podem alterar campos comerciais; preserve
        # a posição canônica calculada para fins de memória e follow-up.
        decision.journey_state = state
        decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS.get(
            state,
            _JOURNEY_PENDING_ACTIONS["DISCOVERY"],
        )
        decision.stagnation_count = 0 if repetitive else decision.stagnation_count

    if not decision.next_action.strip():
        decision.next_action = decision.journey_pending_action
    return decision


def run_ai_decision(agent: AIAgentProfile, context: dict) -> AIDecision:
    if not settings.openai_api_key:
        raise AIConfigurationError("OPENAI_API_KEY não configurada.")

    from agents import Agent, Runner

    instructions = (agent.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    effective_mode = _effective_execution_mode(agent)
    prompt = (
        "Analise esta interação comercial do WhatsApp da Sulnet. "
        "Use o histórico e a base de conhecimento abaixo, respeitando rigorosamente a origem de cada dado. "
        "REGRA DE EVIDÊNCIA: mensagens INBOUND são falas do cliente; mensagens OUTBOUND são falas do próprio SulIA. "
        "Nunca transforme uma frase OUTBOUND em fato sobre o cliente. Nunca conclua provedor, preço atual, velocidade atual, "
        "uso, satisfação, residência ou empresa apenas porque o SulIA mencionou isso antes. A seção customer_evidence contém "
        "somente falas reais do cliente e é a fonte principal para esses fatos. Dados de memória/CRM gerados pela IA não devem "
        "ser tratados como confirmação do cliente. Quando algo não estiver confirmado, pergunte em vez de assumir. "
        "Produza a decisão estruturada do SDR. Antes de qualquer oferta, se lead.confirmed_existing_customer estiver vazio, "
        "a primeira qualificação é perguntar se a pessoa já é cliente Sulnet; cliente existente segue para Telefonia Móvel ou nova conexão para outro endereço, "
        "e não cliente segue para Internet ou Telefonia Móvel. Não repita essa pergunta se a memória já estiver confirmada. "
        "Além da resposta, classifique objeção, estado comercial, plano/oferta atual, nível de concessão N0-N3, meses grátis e se existe compromisso claro de compra. "
        "Atualize também a memória comercial estruturada: customer_needs, benefits_valued, commercial_arguments_used, "
        "proposal_summary e unanswered_question. Registre em commercial_arguments_used somente argumentos realmente usados "
        "na resposta ao cliente, não todos os argumentos disponíveis na base. Se o cliente fizer uma pergunta cuja resposta "
        "não esteja sustentada pela base aprovada nem pelas regras estruturadas, não invente: responda com transparência e "
        "preencha unanswered_question com a dúvida que precisa de validação administrativa. "
        "Sempre que a mensagem fizer uma pergunta com 2 ou 3 respostas previsíveis, preencha quick_replies com "
        "2 ou 3 botões curtos, com no máximo 20 caracteres por label. Exemplos: Para casa/Para empresa; Sim/Não; "
        "800 Mega/650 Mega; Preço/Lentidão/Quedas. Para pergunta realmente aberta, quick_replies pode ficar vazio. "
        "Na primeira apresentação de um plano, responda com no máximo duas frases: cidade, fibra óptica, velocidade, preço mensal e CTA direto para contratar. "
        "Não use a sigla FTTH com o cliente e não mencione espontaneamente fidelidade, multa, instalação, habilitação, upload, roteador ou benefícios. "
        "Não pergunte automaticamente o provedor atual na mesma mensagem da primeira oferta; primeiro teste o fechamento. "
        "Esses detalhes só entram se o cliente perguntar, hesitar ou trouxer objeção. "
        "Nos planos de 800 Mega e 650 Mega há 1 roteador incluso em comodato; cada roteador adicional custa R$ 29,90 por mês. "
        "Se o cliente perguntar sobre quantidade de roteadores, responda diretamente e nunca diga que precisa validar essa informação. "
        "Se o cliente recusar apenas uma informação secundária com 'não, obrigado', 'agora não' ou equivalente, não encerre a conversa: "
        "aceite a recusa daquele detalhe e volte para a oferta principal com CTA de avanço. Nunca responda apenas 'se quiser, me chame' nesse cenário. "
        "Se o cliente rejeitar a própria oferta de internet sem pedir para parar o contato, também não encerre: mude o ângulo e pergunte se ele já possui conexão "
        "de internet em casa ou na empresa, preferencialmente com botões Sim/Não. Nunca diga 'não vou insistir' nesse cenário. "
        "Quando o cliente confirmar que é para empresa/PJ, preserve esse segmento como memória confirmada e use apenas o catálogo corporativo: "
        "Corp. 200 Mega R$ 139,00; Corp. 250 Mega R$ 169,00; Corp. 650 Mega R$ 199,90, todos com IP Fixo, Sulnet Monitora, Wi-Fi 6, ativação gratuita e atendimento prioritário; "
        "250 inclui também Sulnet Play. O diferencial corporativo inclui SLA de atendimento de até 8 horas, suporte nível 2, equipe especializada em empresas, "
        "entrega de banda diferenciada em relação ao residencial e monitoramento proativo do NOC. Não chame de link dedicado e não trate 8 horas como prazo de resolução. "
        "Não aplique promoções residenciais ao corporativo sem regra aprovada. "
        "Quando o cliente pedir todos os planos, catálogo, preços ou velocidades de forma ampla, nunca diga que não possui a lista e nunca estime valores: "
        "se o segmento confirmado for Empresa, liste os corporativos; caso contrário, ofereça primeiro Fibra óptica/Móvel em botões e depois liste somente opções confirmadas do catálogo. "
        "Se o cliente pedir uma velocidade/plano que não esteja disponível na cidade, isso NÃO é motivo de handoff: informe que aquela opção não está disponível, "
        "apresente somente os planos válidos do catálogo para a cidade e continue a venda até o cliente escolher uma opção. Se a cidade ainda não estiver confirmada, pergunte a cidade. "
        "Pedido explícito de humano deve ir para atendimento humano. Intenção clara de contratação em oferta válida deve ir para o Vendedor IA de Fechamento, não para vendedor humano. "
        f"Modo efetivo: {effective_mode}.\n\n"
        + json.dumps(context, ensure_ascii=False, default=str)
    )

    sdr = Agent(
        name=agent.name,
        instructions=instructions,
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 45.0,
        },
        output_type=AIDecision,
    )
    result = Runner.run_sync(sdr, prompt)
    decision = result.final_output
    if not isinstance(decision, AIDecision):
        decision = AIDecision.model_validate(decision)
    decision = _sanitize_unconfirmed_customer_facts(decision, context)
    decision = _apply_catalog_navigation_policy(decision, context)
    decision = _apply_unavailable_plan_policy(decision, context)
    decision = _apply_router_information_policy(decision, context)
    decision = _apply_corporate_customer_policy(decision, context)
    decision = _apply_secondary_decline_reengagement_policy(decision, context)
    decision = _apply_offer_rejection_diagnostic_policy(decision, context)
    decision = _apply_prospecting_entry_policy(decision, context)
    decision = _apply_journey_orchestration_policy(decision, context)
    return _sanitize_unconfirmed_customer_facts(decision, context)


def _send_autonomous_reply(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIJob,
    decision: AIDecision,
) -> Message | None:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        raise AIConfigurationError("Conversa do job não existe.")
    if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return None
    if decision.intent == "DO_NOT_CONTACT":
        return None

    contact = conversation.contact
    phone = (contact.phone if contact else "").strip()
    if not phone:
        raise AIConfigurationError("Contato da conversa não possui telefone.")

    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    zapi = service_for_conversation(db, conversation.id)
    quick_replies = quick_replies_for_message(
        decision.reply_text,
        decision.quick_replies,
    )
    if quick_replies:
        result = zapi.send_button_list(
            phone=phone,
            message=decision.reply_text,
            buttons=quick_replies,
        )
        message_type = "BUTTONS"
    else:
        result = zapi.send_text(
            phone=phone,
            message=decision.reply_text,
        )
        message_type = "TEXT"
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=message_type,
        body=decision.reply_text,
        sender_user_id=agent.user_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    db.flush()
    write_audit(
        db,
        user=agent.user,
        action="AI_SDR_REPLY_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "ai_job_id": job.id,
            "message_id": message.id,
            "provider_message_id": provider_id,
            "handoff_recommended": decision.handoff_recommended,
            "quick_reply_count": len(quick_replies),
        },
    )
    LOG.info(
        "ai_reply_sent job=%s conversation=%s message=%s handoff=%s buttons=%s",
        job.id,
        conversation.id,
        message.id,
        decision.handoff_recommended,
        len(quick_replies),
    )
    return message


def _append_note(existing: str, value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        return existing or ""
    current = (existing or "").strip()
    if clean in current:
        return current
    joined = f"{current}\n[IA SDR] {clean}".strip()
    return joined[-6000:]


def _apply_crm_decision(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIJob,
    decision: AIDecision,
) -> None:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        return
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    if not lead:
        return

    if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
        lead.do_not_contact = True
        lead.stage = "NAO_CONTATAR"
        lead.next_action_at = None
    elif lead.stage not in {"GANHO", "NAO_CONTATAR"}:
        next_stage = decision.recommended_stage
        if next_stage == "NAO_CONTATAR":
            next_stage = "CONTATO_REALIZADO"
        lead.stage = next_stage
        lead.temperature = decision.temperature
        if decision.sales_state == "READY_FOR_HUMAN_SALES":
            lead.priority = "ALTA"

    qualification = decision.qualification
    if qualification.city:
        lead.city = qualification.city.strip()[:120]
    if qualification.address:
        lead.address = qualification.address.strip()[:255]
    if qualification.customer_type in {"EMPRESA", "RESIDENCIAL"}:
        _persist_confirmed_customer_type(
            lead,
            qualification.customer_type,
            conversation_id=conversation.id,
        )
    if qualification.existing_customer in {"CLIENTE", "NAO_CLIENTE"}:
        _persist_confirmed_existing_customer(
            lead,
            qualification.existing_customer,
            conversation_id=conversation.id,
        )
    _persist_journey_memory(
        lead,
        decision,
        conversation_id=conversation.id,
    )

    memory_parts = [
        f"Estado={decision.sales_state}",
        f"intenção={decision.intent}",
        f"objeção={decision.objection}",
        f"plano={decision.offer_plan_code or '-'}",
        f"N{decision.concession_level}",
        f"meses_gratis={decision.free_months}",
        f"segmento_confirmado={qualification.customer_type}",
        f"cliente_sulnet={qualification.existing_customer}",
        f"jornada={decision.journey_state}",
        f"pendencia={decision.journey_pending_action}",
        f"handoff_destino={decision.handoff_target}",
        f"estagnacao={decision.stagnation_count}",
    ]
    if qualification.current_provider:
        memory_parts.append(f"provedor={qualification.current_provider}")
    if qualification.current_speed:
        memory_parts.append(f"velocidade_atual={qualification.current_speed}")
    if qualification.current_price:
        memory_parts.append(f"preco_atual={qualification.current_price}")
    if qualification.desired_product:
        memory_parts.append(f"produto_interesse={qualification.desired_product}")
    if qualification.pain_points:
        memory_parts.append(f"dores={', '.join(qualification.pain_points)}")
    if decision.customer_needs:
        memory_parts.append(f"necessidades={', '.join(decision.customer_needs)}")
    if decision.benefits_valued:
        memory_parts.append(f"beneficios_valorizados={', '.join(decision.benefits_valued)}")
    if decision.commercial_arguments_used:
        memory_parts.append(f"argumentos_usados={', '.join(decision.commercial_arguments_used)}")
    if decision.proposal_summary:
        memory_parts.append(f"proposta={decision.proposal_summary}")

    lead.notes = _append_note(lead.notes, decision.summary)
    lead.potential = _append_note(
        lead.potential,
        "Memória comercial: " + "; ".join(memory_parts),
    )
    lead.last_contact_at = utcnow()
    db.add(
        Activity(
            lead_id=lead.id,
            user_id=agent.user_id,
            kind="IA_SDR",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=decision.sales_state[:120],
            description=(
                f"IA SDR: {decision.summary}\nPróxima ação: {decision.next_action}\n"
                f"Argumentos usados: {', '.join(decision.commercial_arguments_used) or '-'}\n"
                f"Benefícios valorizados: {', '.join(decision.benefits_valued) or '-'}\n"
                f"Proposta: {decision.proposal_summary or '-'}\n"
                f"CRM: {' | '.join(decision.crm_notes)}"
            )[:5000],
            completed_at=utcnow(),
        )
    )


def _perform_handoff(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIJob,
    decision: AIDecision,
) -> None:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        return
    queue = queue_for_conversation(db, conversation.id)
    owner = conversation.assigned_user
    if not owner or owner.role == "AI_SDR":
        owner = assign_queue_user(db, queue) if queue else None
        conversation.assigned_user_id = owner.id if owner else None

    conversation.status = "EM_ATENDIMENTO" if owner else "EM_FILA"
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=owner.id if owner else None,
            assigned_by_id=agent.user_id,
            event="AI_HANDOFF_READY_FOR_SALES",
        )
    )
    if conversation.lead:
        conversation.lead.assigned_user_id = owner.id if owner else conversation.lead.assigned_user_id
        if conversation.lead.stage not in {"GANHO", "NAO_CONTATAR"}:
            conversation.lead.stage = "NEGOCIACAO"
            conversation.lead.priority = "ALTA"

    handoff_summary = (
        f"Resumo SDR: {decision.summary}\n"
        f"Segmento confirmado: {decision.qualification.customer_type}\n"
        f"Já é cliente Sulnet: {decision.qualification.existing_customer}\n"
        f"Plano/serviço: {decision.offer_plan_code or decision.qualification.desired_product or '-'}\n"
        f"Condição: N{decision.concession_level}; meses grátis={decision.free_months}; "
        f"preço ofertado={decision.offer_price if decision.offer_price is not None else '-'}\n"
        f"Provedor atual: {decision.qualification.current_provider or '-'}; "
        f"velocidade atual: {decision.qualification.current_speed or '-'}; "
        f"valor atual: {decision.qualification.current_price or '-'}\n"
        f"Necessidades: {', '.join(decision.customer_needs) or '-'}\n"
        f"Dores: {', '.join(decision.qualification.pain_points) or '-'}\n"
        f"Benefícios valorizados: {', '.join(decision.benefits_valued) or '-'}\n"
        f"Objeção atual: {decision.objection}\n"
        f"Argumentos já usados: {', '.join(decision.commercial_arguments_used) or '-'}\n"
        f"Proposta: {decision.proposal_summary or '-'}\n"
        f"Pendência/próxima ação: {decision.next_action}"
    )
    if conversation.lead:
        db.add(
            Activity(
                lead_id=conversation.lead.id,
                user_id=agent.user_id,
                kind="HANDOFF_IA",
                status="CONCLUIDA",
                channel="WHATSAPP",
                outcome="PRONTO_PARA_FECHAMENTO",
                description=handoff_summary[:5000],
                completed_at=utcnow(),
            )
        )

    from app.services.ai_followups import cancel_pending_followups
    cancel_pending_followups(db, conversation.id, reason="HANDOFF_HUMANO")

    write_audit(
        db,
        user=agent.user,
        action="AI_SDR_HANDOFF",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "queue_id": queue.id if queue else job.queue_id,
            "assigned_user_id": owner.id if owner else None,
            "intent": decision.intent,
            "reason": decision.handoff_reason,
            "offer_plan_code": decision.offer_plan_code,
            "free_months": decision.free_months,
            "qualification": decision.qualification.model_dump(),
            "customer_needs": decision.customer_needs,
            "benefits_valued": decision.benefits_valued,
            "commercial_arguments_used": decision.commercial_arguments_used,
            "proposal_summary": decision.proposal_summary,
            "next_action": decision.next_action,
        },
    )


def process_ai_job(db: Session, job: AIJob) -> str:
    agent = db.get(AIAgentProfile, job.agent_id)
    if not agent or not agent.active:
        job.status = "IGNORADO"
        job.error = "Agente inexistente ou inativo."
        return "skipped"

    if agent.role_type == "CLOSER":
        from app.services.ai_seller import process_seller_job
        return process_seller_job(db, job, agent)

    context = build_ai_context(db, job=job, agent=agent)
    effective_mode = _effective_execution_mode(agent)
    run = AIRun(
        job_id=job.id,
        agent_id=agent.id,
        conversation_id=job.conversation_id,
        message_id=job.message_id,
        execution_mode=effective_mode,
        model=agent.model,
        status="PROCESSANDO",
        input_snapshot_json=json.dumps(context, ensure_ascii=False, default=str),
        started_at=utcnow(),
    )
    db.add(run)
    db.flush()

    try:
        decision = run_ai_decision(agent, context)
    except Exception as exc:
        run.status = "FALHOU"
        run.error = str(exc)[:2000]
        run.finished_at = utcnow()
        job.error = str(exc)[:2000]
        if isinstance(exc, AIConfigurationError):
            job.status = "PENDENTE"
            job.available_at = utcnow() + timedelta(minutes=5)
            job.attempts = max(0, int(job.attempts or 1) - 1)
            return "configuration"
        if int(job.attempts or 0) < 3:
            job.status = "PENDENTE"
            job.available_at = utcnow() + timedelta(seconds=30 * int(job.attempts or 1))
        else:
            job.status = "FALHOU"
        return "failed"

    output = decision.model_dump()
    closer_started = False

    if effective_mode == "AUTO":
        try:
            _apply_crm_decision(
                db,
                agent=agent,
                job=job,
                decision=decision,
            )
            sent_message = None
            wants_closer = (
                decision.handoff_target == "CLOSER_AI"
                or (
                    decision.intent not in {"DO_NOT_CONTACT", "HUMAN_REQUEST"}
                    and decision.sales_state != "OPT_OUT"
                    and (
                        decision.customer_commitment
                        or decision.sales_state == "PURCHASE_INTENT"
                    )
                )
            )
            if wants_closer:
                from app.services.ai_seller import start_closing_from_sdr
                closer_started = start_closing_from_sdr(
                    db,
                    sdr_agent=agent,
                    job=job,
                    decision=decision,
                )
                if not closer_started:
                    # Exceção operacional: se o Vendedor IA estiver indisponível,
                    # não deixar um cliente que já aceitou comprar preso no SDR.
                    decision.handoff_target = "HUMAN"
                    decision.handoff_recommended = True
                    decision.handoff_reason = (
                        "Vendedor IA de Fechamento indisponível no momento."
                    )
                    decision.journey_state = "HUMAN_HANDOFF"
                    decision.journey_pending_action = (
                        "Transferir para atendimento humano por indisponibilidade operacional do Vendedor IA."
                    )
                    decision.reply_text = (
                        "Perfeito! Vou encaminhar seu atendimento para um vendedor "
                        "concluir a contratação com você."
                    )
                    decision.quick_replies = []
                    decision.followup_needed = False
                    fallback_conversation = db.get(Conversation, job.conversation_id)
                    if fallback_conversation and fallback_conversation.lead:
                        _persist_journey_memory(
                            fallback_conversation.lead,
                            decision,
                            conversation_id=fallback_conversation.id,
                        )

            if not closer_started:
                sent_message = _send_autonomous_reply(
                    db,
                    agent=agent,
                    job=job,
                    decision=decision,
                )

            if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
                from app.services.ai_followups import cancel_pending_followups
                cancel_pending_followups(db, job.conversation_id, reason="OPT_OUT")
            elif not closer_started and decision.handoff_target == "HUMAN":
                _perform_handoff(
                    db,
                    agent=agent,
                    job=job,
                    decision=decision,
                )
            elif (
                not closer_started
                and sent_message is not None
                and decision.intent not in {"DO_NOT_CONTACT", "HUMAN_REQUEST"}
                and decision.sales_state != "OPT_OUT"
            ):
                # Toda conversa comercial ativa recebe follow-up persistente.
                # O agendamento não depende mais de o modelo lembrar de marcar
                # followup_needed=True, evitando leads parados por silêncio.
                from app.services.ai_followups import schedule_followup
                schedule_followup(
                    db,
                    agent_id=agent.id,
                    queue_id=job.queue_id,
                    conversation_id=job.conversation_id,
                    source_message_id=job.message_id,
                    step=1,
                    pending_action=(
                        decision.journey_pending_action
                        or decision.next_action
                    ),
                )
        except Exception as exc:
            run.status = "FALHOU"
            run.error = f"Falha no envio automático: {exc}"[:2000]
            run.finished_at = utcnow()
            job.error = run.error
            if int(job.attempts or 0) < 3:
                job.status = "PENDENTE"
                job.available_at = utcnow() + timedelta(seconds=30 * int(job.attempts or 1))
            else:
                job.status = "FALHOU"
            return "send_failed"

    output = decision.model_dump()
    run.status = "CONCLUIDO"
    run.intent = decision.intent
    run.qualification_score = decision.qualification_score
    run.temperature = decision.temperature
    run.confidence = decision.confidence
    run.handoff_recommended = decision.handoff_recommended
    run.handoff_reason = decision.handoff_reason
    run.reply_text = decision.reply_text
    run.summary = decision.summary
    run.output_json = json.dumps(output, ensure_ascii=False, default=str)
    run.finished_at = utcnow()

    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == job.conversation_id
        )
    )
    if closer_started:
        # start_closing_from_sdr já transferiu a propriedade do estado ao closer.
        if state and not state.summary:
            state.summary = decision.summary
    else:
        if not state:
            state = AIConversationState(
                conversation_id=job.conversation_id,
                agent_id=agent.id,
            )
            db.add(state)
        state.agent_id = agent.id
        if effective_mode == "SHADOW":
            state.state = "SHADOW_REVIEW"
        elif decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
            state.state = "OPT_OUT"
        elif decision.handoff_target == "HUMAN":
            state.state = "HANDOFF"
            state.handoff_at = utcnow()
        else:
            state.state = "AI_ACTIVE"
        state.summary = decision.summary
        state.qualification_score = decision.qualification_score
        state.temperature = decision.temperature
        state.last_intent = decision.intent
        state.last_run_id = run.id

    job.status = "CONCLUIDO"
    job.error = None

    write_audit(
        db,
        user=agent.user,
        action="AI_SDR_SHADOW_ANALYZED" if effective_mode == "SHADOW" else "AI_SDR_ANALYZED",
        entity_type="conversation",
        entity_id=job.conversation_id,
        payload={
            "ai_run_id": run.id,
            "agent_id": agent.id,
            "mode": effective_mode,
            "intent": decision.intent,
            "score": decision.qualification_score,
            "temperature": decision.temperature,
            "handoff_recommended": decision.handoff_recommended,
            "handoff_target": decision.handoff_target,
            "journey_state": decision.journey_state,
            "stagnation_count": decision.stagnation_count,
            "confidence": decision.confidence,
        },
    )
    return "completed"


def latest_run_for_conversation(db: Session, conversation_id: int) -> AIRun | None:
    return db.scalar(
        select(AIRun)
        .where(
            AIRun.conversation_id == conversation_id,
            AIRun.status == "CONCLUIDO",
        )
        .order_by(AIRun.created_at.desc(), AIRun.id.desc())
        .limit(1)
    )


def ai_metrics(db: Session, agent_id: int) -> dict[str, int]:
    runs = int(
        db.scalar(
            select(func.count(AIRun.id)).where(
                AIRun.agent_id == agent_id,
                AIRun.status == "CONCLUIDO",
            )
        )
        or 0
    )
    handoffs = int(
        db.scalar(
            select(func.count(AIRun.id)).where(
                AIRun.agent_id == agent_id,
                AIRun.status == "CONCLUIDO",
                AIRun.handoff_recommended.is_(True),
            )
        )
        or 0
    )
    pending = int(
        db.scalar(
            select(func.count(AIJob.id)).where(
                AIJob.agent_id == agent_id,
                AIJob.status == "PENDENTE",
            )
        )
        or 0
    )
    failed = int(
        db.scalar(
            select(func.count(AIJob.id)).where(
                AIJob.agent_id == agent_id,
                AIJob.status == "FALHOU",
            )
        )
        or 0
    )
    return {
        "runs": runs,
        "handoffs": handoffs,
        "pending": pending,
        "failed": failed,
    }
