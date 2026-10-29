from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
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
from app.services.ai_sdr_policy import SULIA_SYSTEM_PROMPT
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
    decision_maker: Literal["SIM", "NAO", "NAO_INFORMADO"] = "NAO_INFORMADO"
    timeframe: str | None = None
    city: str | None = None


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
    if not settings.ai_sdr_enabled:
        return None
    if message.direction != "INBOUND" or message.message_type != "TEXT":
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

    existing = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
    if existing:
        return existing

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
    ranked: list[tuple[int, int, AIKnowledgeItem]] = []
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
        priority_bonus = int((meta.priority if meta else 50) / 20)
        score = semantic_score + priority_bonus
        ranked.append((score, item.id, item))

    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected: list[AIKnowledgeItem] = []
    chars = 0
    max_chars = max(2000, int(agent.max_knowledge_chars or 12000))
    for score, _item_id, item in ranked:
        if selected and query_tokens and score <= 0:
            continue
        size = len(item.content or "") + len(item.title or "")
        if selected and chars + size > max_chars:
            break
        selected.append(item)
        chars += size
        if len(selected) >= 10:
            break

    if not selected:
        selected = items[:5]
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
            "business_name": lead.business_name if lead else "",
            "contact_name": lead.contact_name if lead else "",
            "segment": lead.segment if lead else "",
            "city": lead.city if lead else "",
            "stage": lead.stage if lead else "",
            "score": lead.score if lead else 0,
            "temperature": lead.temperature if lead else "FRIO",
            "notes": lead.notes if lead else "",
            "potential": lead.potential if lead else "",
        },
        "previous_ai_state": {
            "summary": state.summary if state else "",
            "qualification_score": state.qualification_score if state else 0,
            "temperature": state.temperature if state else "FRIO",
            "last_intent": state.last_intent if state else "UNKNOWN",
        },
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
            }
            for item in knowledge
        ],
    }


def _effective_execution_mode(agent: AIAgentProfile) -> str:
    if not settings.ai_autonomous_enabled:
        return "SHADOW"
    return agent.execution_mode


def run_ai_decision(agent: AIAgentProfile, context: dict) -> AIDecision:
    if not settings.openai_api_key:
        raise AIConfigurationError("OPENAI_API_KEY não configurada.")

    from agents import Agent, Runner

    instructions = (agent.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    effective_mode = _effective_execution_mode(agent)
    prompt = (
        "Analise esta interação comercial do WhatsApp da Sulnet. "
        "Use o histórico, o CRM e a base de conhecimento abaixo. "
        "Produza a decisão estruturada do SDR. Além da resposta, classifique objeção, estado comercial, "
        "plano/oferta atual, nível de concessão N0-N3, meses grátis e se existe compromisso claro de compra. "
        "Sempre que a mensagem fizer uma pergunta com 2 ou 3 respostas previsíveis, preencha quick_replies com "
        "2 ou 3 botões curtos, com no máximo 20 caracteres por label. Exemplos: Para casa/Para empresa; Sim/Não; "
        "800 Mega/650 Mega; Preço/Lentidão/Quedas. Para pergunta realmente aberta, quick_replies pode ficar vazio. "
        "Handoff só deve ocorrer com pedido de humano, intenção clara de contratação ou exceção que exija humano. "
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
    return decision


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

    lead.notes = _append_note(lead.notes, decision.summary)
    lead.potential = _append_note(
        lead.potential,
        (
            f"Estado={decision.sales_state}; intenção={decision.intent}; objeção={decision.objection}; "
            f"plano={decision.offer_plan_code or '-'}; N{decision.concession_level}; "
            f"meses_gratis={decision.free_months}"
        ),
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
        },
    )


def process_ai_job(db: Session, job: AIJob) -> str:
    agent = db.get(AIAgentProfile, job.agent_id)
    if not agent or not agent.active:
        job.status = "IGNORADO"
        job.error = "Agente inexistente ou inativo."
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

    if effective_mode == "AUTO":
        try:
            _apply_crm_decision(
                db,
                agent=agent,
                job=job,
                decision=decision,
            )
            sent_message = _send_autonomous_reply(
                db,
                agent=agent,
                job=job,
                decision=decision,
            )
            if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
                from app.services.ai_followups import cancel_pending_followups
                cancel_pending_followups(db, job.conversation_id, reason="OPT_OUT")
            elif decision.handoff_recommended or decision.sales_state == "READY_FOR_HUMAN_SALES":
                _perform_handoff(
                    db,
                    agent=agent,
                    job=job,
                    decision=decision,
                )
            elif decision.followup_needed and sent_message is not None:
                from app.services.ai_followups import schedule_followup
                schedule_followup(
                    db,
                    agent_id=agent.id,
                    queue_id=job.queue_id,
                    conversation_id=job.conversation_id,
                    source_message_id=job.message_id,
                    step=1,
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
    elif decision.handoff_recommended or decision.sales_state == "READY_FOR_HUMAN_SALES":
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
