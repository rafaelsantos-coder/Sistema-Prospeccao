from __future__ import annotations

import json
import logging
import re
import unicodedata
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
from app.services.ai_sdr_policy import FTTH_PLANS, SULIA_SYSTEM_PROMPT
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


def _apply_secondary_decline_reengagement_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
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
    lead = context.get("lead") or {}
    segment = _fold_sales_text(str(lead.get("segment") or ""))
    business_name = _fold_sales_text(str(lead.get("business_name") or ""))
    company_terms = (
        "empresa",
        "empresarial",
        "corporativo",
        "pj",
        "comercio",
        "comercial",
        "loja",
        "clinica",
        "escritorio",
    )
    if any(term in segment for term in company_terms):
        return True
    if any(term in business_name for term in company_terms):
        return True
    for row in reversed((context.get("history") or [])[-8:]):
        body = _fold_sales_text(str(row.get("body") or ""))
        if "sua empresa" in body or "para empresa" in body:
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
    else:
        question = "Entendi. Hoje você já possui conexão de internet na sua casa?"

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
        "Se o cliente recusar apenas uma informação secundária com 'não, obrigado', 'agora não' ou equivalente, não encerre a conversa: "
        "aceite a recusa daquele detalhe e volte para a oferta principal com CTA de avanço. Nunca responda apenas 'se quiser, me chame' nesse cenário. "
        "Se o cliente rejeitar a própria oferta de internet sem pedir para parar o contato, também não encerre: mude o ângulo e pergunte se ele já possui conexão "
        "de internet em casa ou na empresa, preferencialmente com botões Sim/Não. Nunca diga 'não vou insistir' nesse cenário. "
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
    decision = _apply_secondary_decline_reengagement_policy(decision, context)
    return _apply_offer_rejection_diagnostic_policy(decision, context)


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

    memory_parts = [
        f"Estado={decision.sales_state}",
        f"intenção={decision.intent}",
        f"objeção={decision.objection}",
        f"plano={decision.offer_plan_code or '-'}",
        f"N{decision.concession_level}",
        f"meses_gratis={decision.free_months}",
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
                decision.intent not in {"DO_NOT_CONTACT", "HUMAN_REQUEST"}
                and decision.sales_state != "OPT_OUT"
                and (
                    decision.customer_commitment
                    or decision.sales_state == "PURCHASE_INTENT"
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
                sent_message = _send_autonomous_reply(
                    db,
                    agent=agent,
                    job=job,
                    decision=decision,
                )

            if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
                from app.services.ai_followups import cancel_pending_followups
                cancel_pending_followups(db, job.conversation_id, reason="OPT_OUT")
            elif not closer_started and (
                decision.handoff_recommended
                or decision.sales_state == "READY_FOR_HUMAN_SALES"
            ):
                _perform_handoff(
                    db,
                    agent=agent,
                    job=job,
                    decision=decision,
                )
            elif (
                not closer_started
                and decision.followup_needed
                and sent_message is not None
            ):
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
