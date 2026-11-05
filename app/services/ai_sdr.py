from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

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
from app.services.ai_sdr_models import AIConfigurationError, AIDecision, QualificationData
from app.services.ai_sdr_policies import (  # noqa: F401 — reexport para testes e runtime
    _JOURNEY_PENDING_ACTIONS,
    _ad_style_plan_interest,
    _apply_catalog_navigation_policy,
    _apply_corporate_customer_policy,
    _apply_existing_customer_plan_change_policy,
    _apply_journey_orchestration_policy,
    _apply_offer_rejection_diagnostic_policy,
    _apply_prospecting_entry_policy,
    _apply_router_information_policy,
    _apply_secondary_decline_reengagement_policy,
    _apply_repeated_rejection_policy,
    _apply_stagnation_strategy,
    _apply_unavailable_plan_policy,
    _asks_existing_customer,
    _city_from_sales_context,
    _confirmed_facts_from_history,
    _customer_asked_direct_question,
    _customer_inbound_text,
    _customer_type_from_context,
    _customer_type_from_text,
    _eligible_plans_for_city,
    _existing_customer_from_context,
    _existing_customer_from_text,
    _existing_customer_plan_change_requested,
    _explicit_human_request,
    _explicit_purchase_commitment,
    _fast_purchase_decision,
    _fast_purchase_request,
    _fold_sales_text,
    _ftth_plan_by_speed,
    _is_closer_handoff,
    _is_company_context,
    _is_residential_context,
    _journey_integrity_issues,
    _journey_memory_from_context,
    _journey_product_kind,
    _known_city_for_fast_path,
    _last_outbound_text,
    _looks_like_catalog_request,
    _normalized_city,
    _plan_available_in_city,
    _plan_from_sales_context,
    _product_kind_from_text,
    _promote_clear_purchase_to_closing,
    _recent_outbound_messages_for_repetition,
    _requested_plan_speed,
    _sanitize_unconfirmed_customer_facts,
    _sdr_exposes_internal_handoff,
    _sdr_reply_is_stale_repetition,
    _sdr_reply_nearly_identical,
    _sdr_reply_repeats_recent_outbound,
    _service_city_in_text,
    _show_corporate_differentials,
)
from app.services.ai_sdr_policy import SULIA_SYSTEM_PROMPT
from app.services.audit import write_audit
from app.services.queue_routing import (
    AI_WHEN_OFFLINE,
    ai_owns_conversation,
    assign_queue_user,
    human_online_in_queue,
    queue_for_conversation,
)
from app.timeutils import is_past, utcnow


LOG = logging.getLogger("sulnet_ai_sdr")


DEFAULT_SYSTEM_PROMPT = SULIA_SYSTEM_PROMPT


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


def is_ai_test_phone(phone: str | None) -> bool:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return bool(digits) and any(
        len(test) >= 8 and digits.endswith(test[-11:]) for test in settings.ai_test_phones
    )


def _test_phone_binding(db: Session, conversation_id: int, queue) -> Any | None:
    """Vínculo temporário (não gravado) para o telefone de teste numa fila sem IA."""
    conversation = db.get(Conversation, conversation_id)
    contact = getattr(conversation, "contact", None) if conversation else None
    if not contact or not is_ai_test_phone(contact.phone):
        return None
    agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital")
    ) or db.scalar(
        select(AIAgentProfile)
        .where(AIAgentProfile.role_type == "SDR", AIAgentProfile.active.is_(True))
        .limit(1)
    )
    if not agent:
        return None
    LOG.info("ai_test_phone_binding conversation=%s queue=%s", conversation_id, queue.id)
    return SimpleNamespace(
        queue_id=queue.id, agent_id=agent.id, agent=agent, queue=queue,
        handling_mode="AI_FIRST", active=True, fallback_to_human=True,
    )


def binding_for_conversation(db: Session, conversation_id: int) -> AIQueueBinding | None:
    queue = queue_for_conversation(db, conversation_id)
    if not queue:
        return None
    return queue_ai_binding(db, queue.id) or _test_phone_binding(db, conversation_id, queue)


def human_is_attending(db: Session, conversation: Conversation) -> bool:
    """Um vendedor de verdade está atendendo esta conversa: a IA não entra.

    Exige as duas coisas, para não travar a Fila IA: a conversa está atribuída
    a uma pessoa (não a um agente de IA) e essa pessoa escreveu manualmente nas
    últimas 24 horas. Disparos de campanha não contam como atendimento.
    Em 04/11, ao ligar a IA na fila Comercial, a SulIA respondeu dois clientes
    que estavam com o Fabiano e a Claudia (conversas 449 e 422).
    """
    from app.models import CampaignDispatchRecipient, CampaignRecipient  # noqa: PLC0415

    assigned = conversation.assigned_user_id
    if not assigned:
        return False
    ai_users = {user_id for user_id in db.scalars(select(AIAgentProfile.user_id)) if user_id}
    if assigned in ai_users:
        return False
    bodies = [
        (body or "").strip()
        for body in db.scalars(
            select(Message.body).where(
                Message.conversation_id == conversation.id,
                Message.direction == "OUTBOUND",
                Message.sender_user_id == assigned,
                Message.message_type.in_(("TEXT", "BUTTONS")),
                Message.created_at >= utcnow() - timedelta(hours=24),
            )
        )
    ]
    bodies = [body for body in bodies if body]
    if not bodies:
        return False
    automated = {
        (body or "").strip()
        for model in (CampaignRecipient, CampaignDispatchRecipient)
        for body in db.scalars(
            select(model.message_body).where(model.conversation_id == conversation.id)
        )
    }
    return any(body not in automated for body in bodies)


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
    ai_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    if ai_state and ai_state.state == "CLOSING" and settings.ai_seller_enabled:
        closer = db.get(AIAgentProfile, ai_state.agent_id)
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

    if ai_state and ai_state.state in {
        "HANDOFF",
        "HANDOFF_HUMAN",
        "HUMAN_ACTIVE",
        "OPT_OUT",
        "ESCALATED",
        "COMPLETED",
    }:
        LOG.info(
            "ai_job_skipped conversation=%s reason=terminal_state state=%s",
            conversation.id,
            ai_state.state,
        )
        return None

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
    if (
        binding.handling_mode == AI_WHEN_OFFLINE
        and not is_ai_test_phone(getattr(conversation.contact, "phone", None))
        and not ai_owns_conversation(db, conversation.id)
        and human_online_in_queue(db, binding.queue_id)
    ):
        # Fila "IA quando não houver vendedor online": tem vendedor, a conversa é dele.
        LOG.info(
            "ai_job_skipped conversation=%s reason=human_online queue=%s",
            conversation.id,
            binding.queue_id,
        )
        return None
    if not is_ai_test_phone(getattr(conversation.contact, "phone", None)) and human_is_attending(
        db, conversation
    ):
        LOG.info(
            "ai_job_skipped conversation=%s reason=human_attending queue=%s",
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

    # Uma resposta por rajada de mensagens: o SDR lê o histórico inteiro, então
    # o job da mensagem mais nova responde também às anteriores ainda na fila
    # (conversa 63: 4 mensagens da IA em 4 minutos).
    for pending in db.scalars(
        select(AIJob).where(
            AIJob.conversation_id == conversation.id,
            AIJob.agent_id == binding.agent_id,
            AIJob.status == "PENDENTE",
        )
    ):
        pending.status = "IGNORADO"
        pending.error = "AGRUPADA_COM_MENSAGEM_SEGUINTE"

    job = AIJob(
        agent_id=binding.agent_id,
        queue_id=binding.queue_id,
        conversation_id=conversation.id,
        message_id=message.id,
        status="PENDENTE",
        available_at=utcnow() + timedelta(seconds=max(0, settings.ai_sdr_debounce_seconds)),
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
            .limit(max(4, min(limit, 200)))
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


def _confirmed_existing_customer_from_lead(
    lead: Lead | None,
    *,
    conversation_id: int | None = None,
) -> str:
    """Resposta "já é cliente?" guardada no lead.

    Com conversation_id, só vale a resposta dada nesta mesma conversa: numa
    conversa nova a SulIA pergunta de novo em vez de presumir.
    """
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
    if conversation_id is not None and marker.get("conversation_id") != conversation_id:
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


def _journey_memory_from_lead(
    lead: Lead | None,
    *,
    conversation_id: int | None = None,
) -> dict:
    if not lead:
        return {}
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    journey = payload.get("ai_journey")
    if not isinstance(journey, dict):
        return {}
    stored_conversation_id = journey.get("conversation_id")
    if (
        conversation_id is not None
        and stored_conversation_id is not None
        and int(stored_conversation_id) != int(conversation_id)
    ):
        return {}
    return dict(journey)


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

    previous = payload.get("ai_journey")
    previous = previous if isinstance(previous, dict) else {}

    existing_customer = str(decision.qualification.existing_customer or "").upper()
    if existing_customer not in {"CLIENTE", "NAO_CLIENTE"}:
        existing_customer = str(previous.get("existing_customer") or "").upper()
    if existing_customer not in {"CLIENTE", "NAO_CLIENTE"}:
        existing_customer = ""

    customer_type = str(decision.qualification.customer_type or "").upper()
    if customer_type not in {"EMPRESA", "RESIDENCIAL"}:
        customer_type = str(previous.get("customer_type") or "").upper()
    if customer_type not in {"EMPRESA", "RESIDENCIAL"}:
        customer_type = ""

    desired_product = str(decision.qualification.desired_product or "").strip()
    if not desired_product:
        desired_product = str(previous.get("desired_product") or "").strip()

    payload["ai_journey"] = {
        "state": decision.journey_state,
        "pending_action": decision.journey_pending_action or decision.next_action,
        "desired_product": desired_product,
        "customer_type": customer_type,
        "existing_customer": existing_customer,
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
        limit=max(40, int(agent.max_history_messages or 24)),
    )
    fact_history = _conversation_messages(
        db,
        conversation.id,
        limit=max(160, int(agent.max_history_messages or 24)),
    )
    confirmed_facts = _confirmed_facts_from_history(fact_history)

    # Memória factual por conversa: o input do último run guarda os fatos que já
    # haviam sido reconstruídos de falas INBOUND. Isso mantém continuidade mesmo
    # quando a confirmação original já saiu da janela recente de mensagens.
    if state and state.last_run_id:
        previous_run = db.get(AIRun, state.last_run_id)
        if previous_run:
            try:
                previous_snapshot = json.loads(previous_run.input_snapshot_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_snapshot = {}
            previous_facts = (
                previous_snapshot.get("confirmed_facts")
                if isinstance(previous_snapshot, dict)
                else {}
            )
            if isinstance(previous_facts, dict):
                for key in (
                    "existing_customer",
                    "customer_type",
                    "product_kind",
                    "desired_product",
                    "city",
                ):
                    if not confirmed_facts.get(key) and previous_facts.get(key):
                        confirmed_facts[key] = previous_facts[key]
                confirmed_facts["answered_questions"] = sorted(
                    set(confirmed_facts.get("answered_questions") or [])
                    | set(previous_facts.get("answered_questions") or [])
                )

    lead_existing = _confirmed_existing_customer_from_lead(lead, conversation_id=conversation.id)
    if (
        not confirmed_facts.get("existing_customer")
        and lead_existing in {"CLIENTE", "NAO_CLIENTE"}
    ):
        confirmed_facts["existing_customer"] = lead_existing
        confirmed_facts["answered_questions"] = sorted(
            set(confirmed_facts.get("answered_questions") or [])
            | {"EXISTING_CUSTOMER"}
        )

    lead_customer_type = _confirmed_customer_type_from_lead(lead)
    if (
        not confirmed_facts.get("customer_type")
        and lead_customer_type in {"EMPRESA", "RESIDENCIAL"}
    ):
        confirmed_facts["customer_type"] = lead_customer_type
        confirmed_facts["answered_questions"] = sorted(
            set(confirmed_facts.get("answered_questions") or [])
            | {"CUSTOMER_TYPE"}
        )

    from app.services.ai_learning import learning_context_for_conversation
    learning_experiment = learning_context_for_conversation(
        db,
        conversation_id=conversation.id,
        scope="SDR_COPY",
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
        "confirmed_facts": confirmed_facts,
        "learning_experiment": learning_experiment,
        "lead": {
            "id": lead.id if lead else None,
            "contact_name": lead.contact_name if lead else "",
            "city": lead.city if lead else "",
            "stage": lead.stage if lead else "",
            "score": lead.score if lead else 0,
            "temperature": lead.temperature if lead else "FRIO",
            "confirmed_customer_type": _confirmed_customer_type_from_lead(lead),
            "confirmed_existing_customer": _confirmed_existing_customer_from_lead(
                lead, conversation_id=conversation.id
            ),
            "journey": _journey_memory_from_lead(
                lead,
                conversation_id=conversation.id,
            ),
        },
        "previous_ai_state": {
            "qualification_score": state.qualification_score if state else 0,
            "temperature": state.temperature if state else "FRIO",
            "last_intent": state.last_intent if state else "UNKNOWN",
            "state": state.state if state else "NEW",
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


def _official_catalog_for_context(context: dict) -> dict | None:
    """Catálogo da cidade já no contexto, para o modelo não gastar uma ida e volta
    com a ferramenta consultar_catalogo em todo turno (latência de 25-45 s)."""
    from app.services.ai_sdr_policy import catalog_for

    facts = context.get("confirmed_facts") or {}
    city = str(facts.get("city") or (context.get("lead") or {}).get("city") or "").strip()
    customer_type = str(facts.get("customer_type") or "").upper()
    if customer_type == "EMPRESA":
        return catalog_for("", "EMPRESA")
    if not city:
        return None
    return catalog_for(city, "RESIDENCIAL")


_PENALTY_CUES = ("multa", "cancel", "fidelidade", "quebrar", "rescis", "sair antes")


def _tools_needed_for_turn(tools: list[Any], context: dict) -> list[Any]:
    """Só oferece ao modelo as ferramentas que o turno pode precisar.

    Cada chamada de ferramenta é uma ida e volta inteira ao modelo. No
    simulador, o modelo chamava calcular_multa e buscar_manual até em
    "Quero avançar", somando ~10 s por chamada.
    """
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    needed: set[str] = set()
    if not context.get("catalogo_oficial_da_cidade"):
        needed.add("consultar_catalogo")
    if any(cue in latest for cue in _PENALTY_CUES):
        needed.add("calcular_multa")
    if _customer_asked_direct_question(context):
        needed.add("buscar_manual")
    return [tool for tool in tools if getattr(tool, "name", "") in needed]


def run_ai_decision(agent: AIAgentProfile, context: dict) -> AIDecision:
    if not settings.openai_api_key:
        raise AIConfigurationError("OPENAI_API_KEY não configurada.")
    official_catalog = _official_catalog_for_context(context)
    if official_catalog:
        context = {**context, "catalogo_oficial_da_cidade": official_catalog}

    from agents import Agent
    from app.services.ai_runtime import run_agent_sync

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
        "A seção confirmed_facts é memória factual reconstruída das respostas INBOUND do cliente e tem precedência sobre inferências do modelo. "
        "Nunca volte a perguntar uma questão presente em confirmed_facts.answered_questions, salvo se o próprio cliente corrigir explicitamente a resposta. "
        "Uma informação confirmada não pode regredir para NAO_INFORMADO nem fazer a jornada voltar para uma etapa anterior. "
        "Se learning_experiment trouxer instrução, ela pode alterar SOMENTE redação/abordagem dentro do escopo informado. "
        "Ela nunca pode alterar fatos, preço, plano, catálogo, fidelidade, contrato, crédito, estado da jornada ou handoff. "
        "Produza a decisão estruturada do SDR. Antes de qualquer oferta, se lead.confirmed_existing_customer estiver vazio e confirmed_facts.existing_customer também estiver vazio, "
        "a primeira qualificação é perguntar se a pessoa já é cliente Sulnet; cliente existente segue para Telefonia Móvel ou nova conexão para outro endereço, "
        "e não cliente segue para Internet ou Telefonia Móvel. Não repita essa pergunta se a memória já estiver confirmada. "
        "Além da resposta, classifique objeção, estado comercial, plano/oferta atual, nível de concessão N0-N3, meses grátis e se existe compromisso claro de compra. "
        "Atualize também a memória comercial estruturada: customer_needs, benefits_valued, commercial_arguments_used, "
        "proposal_summary e unanswered_question. Registre em commercial_arguments_used somente argumentos realmente usados "
        "na resposta ao cliente, não todos os argumentos disponíveis na base. Se o cliente fizer uma pergunta cuja resposta "
        "não esteja sustentada pela base aprovada nem pelas regras estruturadas, não invente: responda com transparência e "
        "preencha unanswered_question com a dúvida que precisa de validação administrativa. "
        "Se existir anti_repetition.forbidden_previous_messages no contexto, é proibido devolver qualquer uma dessas mensagens novamente. "
        "Mantenha o objetivo comercial e a informação que ainda falta, mas formule a continuação de outra maneira e avance a conversa. "
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

    tools: list[Any] = []
    if settings.ai_sdr_tools_enabled:
        from app.services.ai_sdr_tools import build_sdr_tools

        tools = _tools_needed_for_turn(build_sdr_tools(agent), context)
        prompt = (
            "MULTA: para qualquer pergunta de multa ou cancelamento antecipado, chame "
            "calcular_multa e responda com o valor em reais; nunca mostre fórmula, siglas "
            "como VTB ou MR, nem diga que não consegue calcular. "
            "OBJEÇÃO DE QUALIDADE OU ESTABILIDADE: não prometa ausência de quedas, mas "
            "também não desvenda. Use os diferenciais aprovados do manual (fibra óptica até "
            "a casa, roteador Wi-Fi 6 em comodato, suporte e lojas regionais da Sulnet) e "
            "conduza ao fechamento. Nunca pergunte se o cliente quer contratar 'mesmo sem "
            "garantia'. Quando o cliente disser que topa ou aceita a condição, trate como "
            "intenção de compra.\n"
            "FIDELIDADE: ao tratar objeção de fidelidade, destaque que com fidelidade a taxa de "
            "habilitação de R$ 860 é isenta. "
            "TAMANHO: no WhatsApp, responda em no máximo 3 frases curtas e uma pergunta. "
            "FERRAMENTAS: se catalogo_oficial_da_cidade estiver no contexto, use-o como fonte "
            "oficial de planos e preços e não chame consultar_catalogo; caso contrário, antes de "
            "citar plano, velocidade ou preço, chame consultar_catalogo(cidade, segmento). Use "
            "buscar_manual(pergunta) só quando a resposta não estiver em knowledge. Se a "
            "ferramenta não trouxer a resposta, não invente.\n\n" + prompt
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
        tools=tools,
        output_type=AIDecision,
    )
    model_started = time.monotonic()
    result = run_agent_sync(sdr, prompt)
    model_ms = int((time.monotonic() - model_started) * 1000)
    decision = result.final_output
    if not isinstance(decision, AIDecision):
        decision = AIDecision.model_validate(decision)
    tool_calls: list[str] = []
    tool_facts: list[dict[str, str]] = []
    for item in getattr(result, "new_items", None) or []:
        item_type = getattr(item, "type", "")
        if item_type == "tool_call_item":
            tool_calls.append(str(getattr(getattr(item, "raw_item", None), "name", "") or "?"))
        elif item_type == "tool_call_output_item":
            tool_facts.append(
                {
                    "tool": tool_calls[-1] if tool_calls else "?",
                    "result": str(getattr(item, "output", "") or "")[:3000],
                }
            )
    decision._timing = {
        "model_ms": model_ms,
        "tool_calls": tool_calls,
        "tool_facts": tool_facts,
        "prompt_chars": len(prompt),
        "catalog_in_context": bool(official_catalog),
    }
    decision = _sanitize_unconfirmed_customer_facts(decision, context)
    decision, observations = _apply_rewrite_policies(decision, context)
    decision = _apply_journey_orchestration_policy(decision, context)
    decision = _apply_existing_customer_plan_change_policy(decision, context)
    decision = _promote_clear_purchase_to_closing(decision, context)
    decision = _sanitize_unconfirmed_customer_facts(decision, context)
    decision = _one_question_per_message(decision)
    decision._policy_observations = observations
    LOG.info("sdr_decision_timing %s", json.dumps(decision._timing, ensure_ascii=False))
    return decision


_SENTENCE = re.compile(r"[^?.!\n]*[?.!]+|[^?.!\n]+$")


def _one_question_per_message(decision: AIDecision) -> AIDecision:
    """Uma pergunta por mensagem.

    Em 04/11 a SulIA perguntou "Em qual cidade...? E seria para sua casa ou
    para uma empresa?" com botões Casa/Empresa: quem toca no botão deixa a
    cidade sem resposta. Mantém o texto até a primeira pergunta; os botões
    são refeitos para a pergunta que ficou.
    """
    if _is_closer_handoff(decision):
        return decision
    text = decision.reply_text or ""
    sentences = [item for item in _SENTENCE.findall(text) if item.strip()]
    questions = [index for index, item in enumerate(sentences) if item.rstrip().endswith("?")]
    if len(questions) < 2:
        return decision
    kept = "".join(sentences[: questions[0] + 1]).strip()
    if not kept:
        return decision
    decision.reply_text = kept
    labels = " ".join(item.label for item in decision.quick_replies).lower()
    kept_folded = kept.lower()
    if not any(word in kept_folded for word in labels.split() if len(word) > 3):
        decision.quick_replies = []
    return decision


SDR_REWRITE_POLICIES = (
    ("catalog_navigation", _apply_catalog_navigation_policy),
    ("unavailable_plan", _apply_unavailable_plan_policy),
    ("router_information", _apply_router_information_policy),
    ("corporate_customer", _apply_corporate_customer_policy),
    ("secondary_decline", _apply_secondary_decline_reengagement_policy),
    ("offer_rejection", _apply_offer_rejection_diagnostic_policy),
    ("prospecting_entry", _apply_prospecting_entry_policy),
    ("repeated_rejection", _apply_repeated_rejection_policy),
)


def _observed_policy_names() -> set[str]:
    from app.services.ai_sdr_rewrite import observed_policy_names

    return observed_policy_names(settings.ai_sdr_observe_policies)


def _sdr_script_is_authoritative(decision: AIDecision) -> bool:
    """Resposta já reescrita por regra fixa (catálogo, Black, entrada).

    O Guardião não pode trocar esse texto por um plano único nem cortar o botão.
    """
    return any(
        item.get("mode") == "rewrite" and "reply_text" in (item.get("changed") or [])
        for item in getattr(decision, "_policy_observations", []) or []
    )


def _decision_signature(decision: AIDecision) -> dict[str, Any]:
    from app.services.ai_sdr_rewrite import decision_signature

    return decision_signature(decision)


def _apply_rewrite_policies(
    decision: AIDecision,
    context: dict,
) -> tuple[AIDecision, list[dict[str, Any]]]:
    """Aplica as regras fixas que reescrevem a resposta, medindo cada uma.

    Regras listadas em AI_SDR_OBSERVE_POLICIES só registram o que mudariam; as
    demais continuam aplicadas. O registro vai para o AIRun e permite desligar
    cada regra com base em dados, sem novo deploy.
    """
    from app.services.ai_sdr_rewrite import apply_rewrite_policies

    return apply_rewrite_policies(
        decision,
        context,
        SDR_REWRITE_POLICIES,
        observe=_observed_policy_names(),
    )


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

    current_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    if current_state and (
        current_state.state != "AI_ACTIVE"
        or int(current_state.agent_id or 0) != int(agent.id)
    ):
        LOG.warning(
            "ai_sdr_send_blocked_by_owner conversation=%s agent=%s owner_agent=%s state=%s",
            conversation.id,
            agent.id,
            current_state.agent_id,
            current_state.state,
        )
        raise AIConfigurationError(
            f"SDR perdeu a propriedade da conversa: {current_state.state}"
        )

    if _sdr_exposes_internal_handoff(decision.reply_text):
        raise AIConfigurationError(
            "SDR não pode anunciar Vendedor IA ao cliente; handoff deve ser operacional e silencioso."
        )

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
    if decision.service_redirect:
        # Upgrade, suporte e financeiro: botão que abre o WhatsApp da central.
        from app.services.ai_sdr_policy import CENTRAL_BUTTON_LABEL, CENTRAL_WHATSAPP_URL
        from app.services.zapi import ZApiError

        try:
            result = zapi.send_link_button(
                phone=phone,
                message=decision.reply_text,
                label=CENTRAL_BUTTON_LABEL,
                url=CENTRAL_WHATSAPP_URL,
            )
            message_type = "BUTTONS"
        except ZApiError:
            LOG.warning("central_link_button_failed conversation=%s", conversation.id)
            result = zapi.send_text(
                phone=phone,
                message=f"{decision.reply_text}\n{CENTRAL_WHATSAPP_URL}",
            )
            message_type = "TEXT"
    elif quick_replies:
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


HUMAN_FALLBACK_NOTICE = (
    "Quero te responder com precisão, então vou chamar um consultor da Sulnet "
    "para continuar com você por aqui. Ele já vê toda a nossa conversa."
)


def _send_human_fallback(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIJob,
    run: AIRun,
    decision: AIDecision,
    reason: str,
) -> str:
    """Depois de tentativas sem resposta aprovada, passa para um humano com aviso.

    Antes, o job era reagendado a cada 2 minutos indefinidamente (Guardião) ou
    falhava em silêncio (integridade), e o cliente ficava sem resposta.
    """
    fallback = decision.model_copy(deep=True)
    fallback.reply_text = HUMAN_FALLBACK_NOTICE
    fallback.quick_replies = []
    fallback.handoff_target = "HUMAN"
    fallback.handoff_recommended = True
    fallback.handoff_reason = reason[:600]
    fallback.sales_state = "READY_FOR_HUMAN_SALES"
    fallback.journey_state = "HUMAN_HANDOFF"
    fallback.customer_commitment = False
    fallback.followup_needed = False

    _apply_crm_decision(db, agent=agent, job=job, decision=fallback)
    message = _send_autonomous_reply(db, agent=agent, job=job, decision=fallback)
    _perform_handoff(db, agent=agent, job=job, decision=fallback)

    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == job.conversation_id
        )
    )
    if not state:
        state = AIConversationState(conversation_id=job.conversation_id, agent_id=agent.id)
        db.add(state)
    state.state = "HANDOFF"
    state.handoff_at = utcnow()
    state.last_run_id = run.id

    output = fallback.model_dump()
    output["human_fallback_reason"] = reason
    run.status = "CONCLUIDO"
    run.error = reason[:2000]
    run.reply_text = fallback.reply_text
    run.output_json = json.dumps(output, ensure_ascii=False, default=str)
    run.finished_at = utcnow()
    job.status = "CONCLUIDO"
    job.error = reason[:2000]
    write_audit(
        db,
        user=agent.user,
        action="AI_SDR_HUMAN_FALLBACK",
        entity_type="conversation",
        entity_id=job.conversation_id,
        payload={
            "ai_run_id": run.id,
            "reason": reason[:500],
            "message_id": message.id if message else None,
        },
    )
    LOG.warning(
        "ai_sdr_human_fallback job=%s conversation=%s reason=%s",
        job.id,
        job.conversation_id,
        reason[:300],
    )
    return "human_fallback"


def _try_fast_purchase_path(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIJob,
    run: AIRun,
    context: dict,
) -> str | None:
    request = _fast_purchase_request(context)
    if not request:
        return None
    plan, city = request["plan"], request["city"]
    if city and not _plan_available_in_city(plan, city):
        # Cidade fora da cobertura deste plano: o fluxo normal apresenta as opções.
        return None

    from app.services.ai_seller import start_closing_from_sdr

    handoff = bool(city)
    decision = _fast_purchase_decision(plan, city, handoff=handoff)
    try:
        with db.begin_nested():
            if handoff:
                if not start_closing_from_sdr(db, sdr_agent=agent, job=job, decision=decision):
                    raise AIConfigurationError("Vendedor IA indisponível para o caminho rápido.")
            else:
                _apply_crm_decision(db, agent=agent, job=job, decision=decision)
                _send_autonomous_reply(db, agent=agent, job=job, decision=decision)
                from app.services.ai_followups import schedule_followup

                schedule_followup(
                    db,
                    agent_id=agent.id,
                    queue_id=job.queue_id,
                    conversation_id=job.conversation_id,
                    source_message_id=job.message_id,
                    step=1,
                    pending_action=decision.journey_pending_action,
                )
    except Exception as exc:
        # Qualquer falha devolve a mensagem ao fluxo normal, sem estado parcial.
        LOG.warning(
            "ai_sdr_fast_purchase_fallback job=%s conversation=%s error=%s",
            job.id,
            job.conversation_id,
            exc,
        )
        return None

    run.status = "CONCLUIDO"
    run.intent = decision.intent
    run.qualification_score = decision.qualification_score
    run.temperature = decision.temperature
    run.confidence = decision.confidence
    run.handoff_recommended = decision.handoff_recommended
    run.handoff_reason = decision.handoff_reason
    run.reply_text = "" if handoff else decision.reply_text
    run.summary = decision.summary
    run.output_json = json.dumps(
        {**decision.model_dump(), "fast_path": "HANDOFF" if handoff else "ASK_CITY"},
        ensure_ascii=False,
        default=str,
    )
    run.finished_at = utcnow()
    job.status = "CONCLUIDO"
    job.error = None
    LOG.info(
        "ai_sdr_fast_purchase job=%s conversation=%s plan=%s city=%s handoff=%s",
        job.id,
        job.conversation_id,
        plan.get("code"),
        city or "-",
        handoff,
    )
    return "fast_handoff" if handoff else "fast_ask_city"



def process_ai_job(db: Session, job: AIJob) -> str:
    agent = db.get(AIAgentProfile, job.agent_id)
    if not agent or not agent.active:
        job.status = "IGNORADO"
        job.error = "Agente inexistente ou inativo."
        return "skipped"

    current_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == job.conversation_id
        )
    )
    if (
        agent.role_type != "CLOSER"
        and current_state
        and current_state.state in {
            "CLOSING",
            "HANDOFF",
            "HANDOFF_HUMAN",
            "HUMAN_ACTIVE",
            "OPT_OUT",
            "ESCALATED",
            "COMPLETED",
        }
    ):
        job.status = "IGNORADO"
        job.error = f"Conversa já saiu do SDR: {current_state.state}"
        LOG.info(
            "ai_sdr_job_ignored_after_handoff job=%s conversation=%s state=%s",
            job.id,
            job.conversation_id,
            current_state.state,
        )
        return "skipped"

    if agent.role_type == "CLOSER":
        from app.services.ai_seller import process_seller_job
        return process_seller_job(db, job, agent)

    # Mensagem que entrou antes de "Pausar IA" na fila não recebe resposta do SDR.
    if binding_for_conversation(db, job.conversation_id) is None:
        job.status = "IGNORADO"
        job.error = "IA pausada na fila da conversa."
        LOG.info(
            "ai_sdr_job_ignored_queue_paused job=%s conversation=%s",
            job.id,
            job.conversation_id,
        )
        return "skipped"

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

    if effective_mode == "AUTO":
        fast_outcome = _try_fast_purchase_path(
            db,
            agent=agent,
            job=job,
            run=run,
            context=context,
        )
        if fast_outcome:
            return fast_outcome

    try:
        decision = run_ai_decision(agent, context)
        integrity_issues = _journey_integrity_issues(decision, context)
        if integrity_issues:
            retry_context = dict(context)
            retry_context["integrity_guard"] = {
                "issues": integrity_issues,
                "instruction": (
                    "A primeira decisão violou a integridade da jornada. "
                    "Preserve confirmed_facts, não volte a perguntas já respondidas, "
                    "não repita mensagens anteriores e avance somente para a próxima "
                    "informação realmente pendente."
                ),
            }
            retry_context["anti_repetition"] = {
                "forbidden_previous_messages": _recent_outbound_messages_for_repetition(
                    context
                ),
                "reason": (
                    "A primeira decisão repetiu ou regrediu uma etapa já concluída."
                ),
            }
            LOG.warning(
                "ai_sdr_integrity_regeneration job=%s conversation=%s issues=%s",
                job.id,
                job.conversation_id,
                ",".join(integrity_issues),
            )
            decision = run_ai_decision(agent, retry_context)
            integrity_issues = _journey_integrity_issues(decision, context)

        if (
            integrity_issues
            and effective_mode == "AUTO"
            and int(job.attempts or 0) >= 3
        ):
            return _send_human_fallback(
                db,
                agent=agent,
                job=job,
                run=run,
                decision=decision,
                reason="Integridade da jornada bloqueou as respostas: " + ", ".join(integrity_issues),
            )

        if integrity_issues:
            run.status = "FALHOU"
            run.error = (
                "Integridade da jornada bloqueou a decisão antes do envio: "
                + ", ".join(integrity_issues)
            )[:2000]
            run.finished_at = utcnow()
            job.error = run.error
            if int(job.attempts or 0) < 3:
                job.status = "PENDENTE"
                job.available_at = utcnow() + timedelta(minutes=2)
                job.locked_at = None
            else:
                job.status = "FALHOU"
            LOG.error(
                "ai_sdr_integrity_blocked job=%s conversation=%s attempts=%s issues=%s",
                job.id,
                job.conversation_id,
                job.attempts,
                ",".join(integrity_issues),
            )
            return "integrity_blocked"
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
    output["policy_observations"] = list(decision._policy_observations)
    output["timing"] = dict(decision._timing)
    closer_started = False

    if effective_mode == "AUTO":
        try:
            conversation = db.get(Conversation, job.conversation_id)
            if not conversation:
                raise AIConfigurationError("Conversa do job não existe.")

            from app.services.ai_guardian import guard_outbound_message

            if _is_closer_handoff(decision):
                # A mensagem do SDR não chega ao cliente quando o Vendedor IA
                # assume: revisá-la só atrasava e bloqueava o fechamento
                # (conversa 239 em 04/11 e simulações).
                from app.services.ai_guardian import GuardianResult

                guardian = GuardianResult(
                    verdict="APPROVE",
                    risk="LOW",
                    confidence=1.0,
                    quality_score=100,
                    rationale="Handoff ao Vendedor IA: a mensagem do SDR não é enviada.",
                    reply_text=decision.reply_text,
                    buttons=[],
                    should_send=True,
                )
            else:
                guardian = guard_outbound_message(
                    db,
                    agent=agent,
                    conversation=conversation,
                    reply_text=decision.reply_text,
                    buttons=[item.model_dump() for item in decision.quick_replies],
                    source="SDR_DECISION",
                    proposed_state=decision.journey_state or decision.sales_state,
                    metadata={
                        "intent": decision.intent,
                        "handoff_target": decision.handoff_target,
                        "customer_commitment": decision.customer_commitment,
                        "tool_facts": list(decision._timing.get("tool_facts") or []),
                        "authoritative_sdr_script": _sdr_script_is_authoritative(decision),
                    },
                )
            if not guardian.should_send:
                retry_context = dict(context)
                retry_context["guardian_feedback"] = {
                    "issue_codes": guardian.issue_codes,
                    "rationale": guardian.rationale,
                    "blocked_reply": decision.reply_text,
                    "instruction": (
                        "Gere outra resposta corrigindo os problemas apontados pelo Guardião. "
                        "Não repita a mensagem bloqueada e não altere fatos comerciais sem evidência."
                    ),
                }
                decision = run_ai_decision(agent, retry_context)
                guardian = guard_outbound_message(
                    db,
                    agent=agent,
                    conversation=conversation,
                    reply_text=decision.reply_text,
                    buttons=[item.model_dump() for item in decision.quick_replies],
                    source="SDR_DECISION_RETRY",
                    proposed_state=decision.journey_state or decision.sales_state,
                    metadata={
                        "intent": decision.intent,
                        "handoff_target": decision.handoff_target,
                        "customer_commitment": decision.customer_commitment,
                        "tool_facts": list(decision._timing.get("tool_facts") or []),
                        "authoritative_sdr_script": _sdr_script_is_authoritative(decision),
                    },
                )

            if not guardian.should_send and int(job.attempts or 0) >= 2:
                return _send_human_fallback(
                    db,
                    agent=agent,
                    job=job,
                    run=run,
                    decision=decision,
                    reason=(
                        "Guardião bloqueou as respostas geradas: "
                        + (", ".join(guardian.issue_codes) or guardian.rationale)
                    ),
                )

            if not guardian.should_send:
                run.status = "FALHOU"
                run.error = (
                    "Guardião bloqueou a decisão antes do envio: "
                    + (", ".join(guardian.issue_codes) or guardian.rationale)
                )[:2000]
                run.finished_at = utcnow()
                job.status = "PENDENTE"
                job.error = run.error
                job.available_at = utcnow() + timedelta(minutes=2)
                job.locked_at = None
                return "guardian_blocked"

            if guardian.verdict in {"REWRITE", "CORRECT_STATE"}:
                decision.reply_text = guardian.reply_text
                decision.quick_replies = [
                    QuickReply.model_validate(item)
                    for item in guardian.buttons
                ]

            post_guard_issues = _journey_integrity_issues(decision, context)
            if post_guard_issues and int(job.attempts or 0) >= 3:
                return _send_human_fallback(
                    db,
                    agent=agent,
                    job=job,
                    run=run,
                    decision=decision,
                    reason="Integridade bloqueou a saída do Guardião: " + ", ".join(post_guard_issues),
                )
            if post_guard_issues:
                run.status = "FALHOU"
                run.error = (
                    "Integridade da jornada bloqueou a saída final do Guardião: "
                    + ", ".join(post_guard_issues)
                )[:2000]
                run.finished_at = utcnow()
                job.error = run.error
                if int(job.attempts or 0) < 3:
                    job.status = "PENDENTE"
                    job.available_at = utcnow() + timedelta(minutes=2)
                    job.locked_at = None
                else:
                    job.status = "FALHOU"
                LOG.error(
                    "ai_sdr_integrity_post_guard_block job=%s conversation=%s issues=%s",
                    job.id,
                    job.conversation_id,
                    ",".join(post_guard_issues),
                )
                return "integrity_blocked"

            output = decision.model_dump()
            output["policy_observations"] = list(decision._policy_observations)
            output["timing"] = dict(decision._timing)

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

            if decision.service_redirect:
                from app.services.ai_followups import cancel_pending_followups
                cancel_pending_followups(db, job.conversation_id, reason="EXISTING_CUSTOMER_PLAN_CHANGE")
                conversation.status = "RESOLVIDO"
                conversation.closed_at = utcnow()
            elif decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
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
                and not decision.service_redirect
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
    output["policy_observations"] = list(decision._policy_observations)
    output["timing"] = dict(decision._timing)
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
        elif decision.service_redirect:
            state.state = "COMPLETED"
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
