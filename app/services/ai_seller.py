from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Activity,
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIJob,
    AIKnowledgeItem,
    AIRun,
    Conversation,
    ConversationAssignment,
    Lead,
    Message,
)
from app.services.audit import write_audit
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
from app.services.queue_routing import assign_queue_user, queue_for_conversation
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_seller")


class SellerInterpretation(BaseModel):
    confirmation: Literal["SIM", "NAO", "NAO_INFORMADO"] = "NAO_INFORMADO"
    requested_plan: str | None = Field(default=None, max_length=180)
    payment_method: str | None = Field(default=None, max_length=100)
    due_day: int | None = Field(default=None, ge=1, le=31)
    invoice_delivery: str | None = Field(default=None, max_length=100)
    human_requested: bool = False
    wants_contest: bool = False


def seller_operational() -> bool:
    return bool(
        settings.ai_seller_enabled
        and settings.ai_autonomous_enabled
        and settings.openai_api_key
        and settings.sulnet_crm_base_url
        and settings.sulnet_ai_integration_key
    )


def active_closer(db: Session) -> AIAgentProfile | None:
    if not seller_operational():
        return None
    return db.scalar(
        select(AIAgentProfile).where(
            AIAgentProfile.role_type == "CLOSER",
            AIAgentProfile.active.is_(True),
        ).order_by(AIAgentProfile.id.asc())
    )


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFD", (value or "").strip().lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _redact(value: str) -> str:
    text = value or ""
    text = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "[DOCUMENTO]", text)
    text = re.sub(r"(?<!\d)\d{5}-?\d{3}(?!\d)", "[CEP]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    return text[:1200]


def _load_collected(state: AIClosingState) -> dict[str, Any]:
    try:
        parsed = json.loads(state.collected_json or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_collected(state: AIClosingState, collected: dict[str, Any]) -> None:
    state.collected_json = json.dumps(collected, ensure_ascii=False, default=str)


def _looks_like_full_name(value: str) -> bool:
    clean = re.sub(r"\s+", " ", (value or "").strip())
    if len(clean) < 5 or len(clean) > 120:
        return False
    parts = [p for p in clean.split(" ") if p]
    return len(parts) >= 2 and all(any(ch.isalpha() for ch in p) for p in parts)


def _extract_name(text: str) -> str | None:
    clean = re.sub(r"^(meu nome (e|é)|sou|aqui e|aqui é)\s+", "", (text or "").strip(), flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" .,!;:")
    return clean if _looks_like_full_name(clean) else None


def _extract_cpf(text: str) -> str | None:
    digits = _digits(text)
    if len(digits) in {11, 14}:
        return digits
    candidates = re.findall(r"(?:\d[.\-\s]?){11,14}", text or "")
    for item in candidates:
        d = _digits(item)
        if len(d) in {11, 14}:
            return d
    return None


def _extract_cep(text: str) -> str | None:
    for item in re.findall(r"\d{5}-?\d{3}", text or ""):
        d = _digits(item)
        if len(d) == 8:
            return d
    digits = _digits(text)
    return digits if len(digits) == 8 else None


def _extract_email(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text or "")
    return match.group(0).lower() if match else None


def _extract_address_number(text: str) -> tuple[str | None, str]:
    value = (text or "").strip()
    match = re.search(r"\b(?:numero|n[uú]mero|n[º°o]?\.?\s*)?(\d{1,7}[A-Za-z]?)\b", value, re.I)
    number = match.group(1) if match else None
    complement = ""
    comp = re.search(
        r"\b(ap(?:to|artamento)?\.?\s*\w+|bloco\s+\w+|casa\s+\w+|sala\s+\w+|fundos|frente|lote\s+\w+)\b",
        value,
        re.I,
    )
    if comp:
        complement = comp.group(1).strip()
    elif number and "," in value:
        complement = value.split(",", 1)[1].strip()
    return number, complement


def _yes_no(text: str) -> str:
    n = _norm(text)
    if re.search(r"\b(sim|confirmo|confirmado|pode|correto|certo|isso|fechado|vamos)\b", n):
        return "SIM"
    if re.search(r"\b(nao|errado|trocar|mudar|outro|outra)\b", n):
        return "NAO"
    return "NAO_INFORMADO"


def _billing_local(text: str) -> dict[str, Any]:
    n = _norm(text)
    result: dict[str, Any] = {}
    methods = [
        ("debito em conta", "Débito em conta"),
        ("cartao", "Cartão"),
        ("boleto", "Boleto"),
        ("pix", "PIX"),
    ]
    for token, label in methods:
        if token in n:
            result["paymentMethod"] = label
            break
    day = re.search(r"\b(?:dia|vencimento(?:\s+no)?(?:\s+dia)?)\s*(\d{1,2})\b", n)
    if day:
        value = int(day.group(1))
        if 1 <= value <= 31:
            result["dueDay"] = value
    if "email" in n or "e-mail" in n:
        result["invoiceDelivery"] = "E-mail"
    elif "whatsapp" in n or "whats" in n:
        result["invoiceDelivery"] = "WhatsApp"
    return result


def _seller_knowledge(db: Session, agent: AIAgentProfile) -> str:
    items = list(
        db.scalars(
            select(AIKnowledgeItem).where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.active.is_(True),
            ).order_by(AIKnowledgeItem.updated_at.desc()).limit(20)
        )
    )
    text = "\n".join(f"- {item.title}: {item.content}" for item in items)
    return text[:6000]


def _interpret_ambiguous(
    db: Session,
    *,
    agent: AIAgentProfile,
    step: str,
    message: str,
    plan_label: str,
) -> SellerInterpretation:
    if not settings.openai_api_key:
        return SellerInterpretation()
    from agents import Agent, Runner

    instructions = (agent.system_prompt or "").strip()
    knowledge = _seller_knowledge(db, agent)
    prompt = (
        "Você está interpretando uma resposta curta no fechamento de uma contratação Sulnet. "
        "Não invente dados. Extraia somente o que o cliente realmente informou. "
        "Nunca trate silêncio/ambiguidade como confirmação. "
        f"Etapa atual: {step}. Plano em negociação: {plan_label or 'não informado'}.\n"
        f"Mensagem do cliente: {_redact(message)}\n"
        f"Conhecimento autorizado:\n{knowledge}"
    )
    interpreter = Agent(
        name=f"{agent.name} · interpretação",
        instructions=instructions or "Interprete respostas de fechamento com conservadorismo.",
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 30.0,
        },
        output_type=SellerInterpretation,
    )
    try:
        result = Runner.run_sync(interpreter, prompt)
        output = result.final_output
        return output if isinstance(output, SellerInterpretation) else SellerInterpretation.model_validate(output)
    except Exception:
        LOG.exception("seller_interpretation_failed step=%s", step)
        return SellerInterpretation()


def _send_text(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    text: str,
) -> Message:
    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    if not text.strip():
        raise ValueError("Mensagem vazia.")
    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone:
        raise CRMIntegrationError("Contato sem telefone para WhatsApp.")
    zapi = service_for_conversation(db, conversation.id)
    result = zapi.send_text(phone=phone, message=text)
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=text,
        sender_user_id=agent.user_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    db.flush()
    return message


def _record_activity(
    db: Session,
    *,
    conversation: Conversation,
    agent: AIAgentProfile,
    outcome: str,
    description: str,
) -> None:
    if not conversation.lead_id:
        return
    db.add(
        Activity(
            lead_id=conversation.lead_id,
            user_id=agent.user_id,
            kind="IA_VENDEDOR",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=outcome[:120],
            description=description[:5000],
            completed_at=utcnow(),
        )
    )


def _create_crm_if_ready(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
) -> None:
    if state.opportunity_id:
        return
    collected = _load_collected(state)
    name = str(collected.get("name") or "").strip()
    phone = _digits(str(collected.get("phone") or conversation.contact.phone or ""))
    if not _looks_like_full_name(name) or len(phone) < 10:
        return

    client = SulnetCRMClient.from_settings()
    payload: dict[str, Any] = {
        "externalId": state.external_id,
        "conversationId": conversation.id,
        "leadId": conversation.lead_id,
        "clientName": name,
        "clientPhone": phone,
        "planLabel": state.plan_label or None,
        "monthlyValue": state.offer_price,
        "sdrSummary": state.handoff_summary,
        "handoffReason": collected.get("handoffReason") or "",
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    result = client.create_opportunity(payload)
    state.opportunity_id = int(result.get("opportunityId") or 0) or None
    state.protocol = str(result.get("protocol") or "")
    if not state.opportunity_id:
        raise CRMIntegrationError("CRM não retornou o identificador da oportunidade.")


def _prompt_for_step(state: AIClosingState, collected: dict[str, Any]) -> str:
    plan = state.plan_label or "plano combinado"
    if state.step == "NAME":
        return f"Perfeito! Vamos dar início à contratação do {plan}. Para começar, me confirma seu nome completo?"
    if state.step == "CPF":
        return "Ótimo. Agora preciso do seu CPF para realizar a consulta cadastral e seguir com a contratação."
    if state.step == "CEP":
        return "Consulta aprovada! Qual é o CEP do endereço onde será instalada a internet?"
    if state.step == "ADDRESS_NUMBER":
        street = str(collected.get("street") or "").strip()
        city = str(collected.get("city") or "").strip()
        prefix = f"Localizei {street}" if street else "Localizei o endereço"
        if city:
            prefix += f", em {city}"
        return prefix + ". Qual é o número e, se houver, o complemento?"
    if state.step == "PLAN_CONFIRM":
        return f"Perfeito. O plano negociado ficou {plan}. Confirma que seguimos com ele?"
    if state.step == "EMAIL":
        return "Certo. Qual e-mail você quer deixar no cadastro e receber as comunicações da contratação?"
    if state.step == "BILLING":
        return "Agora falta o faturamento: como prefere pagar/receber a fatura e qual dia de vencimento você prefere?"
    if state.step == "CREDIT_REJECTED":
        return "A consulta cadastral não foi aprovada automaticamente. Se quiser, posso encaminhar para análise de um atendente."
    if state.step == "WAITING_CONTRACT":
        return "Seu cadastro comercial já foi concluído e o contrato está sendo preparado. Assim que o link de assinatura estiver disponível, eu envio por aqui."
    if state.step == "WAITING_SIGNATURE":
        return "Seu contrato já foi enviado. Assim que você concluir a assinatura, eu confirmo a contratação por aqui."
    return "Vou seguir com a próxima etapa da sua contratação."


def _handoff_to_human(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
    agent: AIAgentProfile,
    reason: str,
) -> None:
    queue = queue_for_conversation(db, conversation.id)
    owner = assign_queue_user(db, queue) if queue else None
    if owner and owner.id == agent.user_id:
        owner = None
    conversation.assigned_user_id = owner.id if owner else None
    conversation.status = "EM_ATENDIMENTO" if owner else "EM_FILA"
    if conversation.lead:
        conversation.lead.assigned_user_id = owner.id if owner else conversation.lead.assigned_user_id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=owner.id if owner else None,
            assigned_by_id=agent.user_id,
            event="AI_CLOSER_HANDOFF_HUMAN",
        )
    )
    state.step = "ESCALATED"
    state.last_error = reason[:1000]
    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
    )
    if ai_state:
        ai_state.state = "HANDOFF_HUMAN"
        ai_state.handoff_at = utcnow()
    write_audit(
        db,
        user=agent.user,
        action="AI_CLOSER_HANDOFF_HUMAN",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"assigned_user_id": owner.id if owner else None, "reason": reason[:300]},
    )


def start_closing_from_sdr(
    db: Session,
    *,
    sdr_agent: AIAgentProfile,
    job: AIJob,
    decision: Any,
) -> bool:
    closer = active_closer(db)
    if not closer:
        return False
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        return False

    plan_label = str(
        getattr(decision, "offer_plan_code", None)
        or getattr(getattr(decision, "qualification", None), "desired_product", None)
        or ""
    ).strip()
    if plan_label.isdigit():
        plan_label += " Mega"
    if not plan_label:
        plan_label = "plano combinado"

    state = db.scalar(
        select(AIClosingState).where(AIClosingState.conversation_id == conversation.id)
    )
    if not state:
        state = AIClosingState(
            conversation_id=conversation.id,
            agent_id=closer.id,
            external_id=f"prospecta-conversation-{conversation.id}",
            plan_label=plan_label,
            offer_price=getattr(decision, "offer_price", None),
            handoff_summary=str(getattr(decision, "summary", "") or ""),
        )
        db.add(state)
        db.flush()
    else:
        state.agent_id = closer.id
        if not state.plan_label:
            state.plan_label = plan_label
        if state.offer_price is None:
            state.offer_price = getattr(decision, "offer_price", None)
        if not state.handoff_summary:
            state.handoff_summary = str(getattr(decision, "summary", "") or "")

    collected = _load_collected(state)
    phone = _digits(conversation.contact.phone if conversation.contact else "")
    if phone:
        collected["phone"] = phone
    known_name = ""
    if conversation.lead and _looks_like_full_name(conversation.lead.contact_name or ""):
        known_name = conversation.lead.contact_name.strip()
    elif conversation.contact and _looks_like_full_name(conversation.contact.name or ""):
        known_name = conversation.contact.name.strip()
    if known_name:
        collected["name"] = known_name
    collected["handoffReason"] = str(getattr(decision, "handoff_reason", "") or "")
    _save_collected(state, collected)

    state.step = "CPF" if collected.get("name") else "NAME"
    conversation.assigned_user_id = closer.user_id
    conversation.status = "EM_ATENDIMENTO"
    if conversation.lead:
        conversation.lead.assigned_user_id = closer.user_id
        if conversation.lead.stage not in {"GANHO", "NAO_CONTATAR"}:
            conversation.lead.stage = "NEGOCIACAO"
            conversation.lead.priority = "ALTA"

    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=closer.user_id,
            assigned_by_id=sdr_agent.user_id,
            event="AI_HANDOFF_TO_CLOSER",
        )
    )
    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
    )
    if not ai_state:
        ai_state = AIConversationState(
            conversation_id=conversation.id,
            agent_id=closer.id,
            state="CLOSING",
            summary=state.handoff_summary,
            qualification_score=100,
            temperature="QUENTE",
            last_intent="PURCHASE_INTENT",
            handoff_at=utcnow(),
        )
        db.add(ai_state)
    else:
        ai_state.agent_id = closer.id
        ai_state.state = "CLOSING"
        ai_state.handoff_at = utcnow()
        ai_state.last_intent = "PURCHASE_INTENT"

    from app.services.ai_followups import cancel_pending_followups
    cancel_pending_followups(db, conversation.id, reason="HANDOFF_VENDEDOR_IA")

    try:
        _create_crm_if_ready(db, state=state, conversation=conversation)
    except CRMIntegrationError as exc:
        state.last_error = str(exc)
        LOG.warning("closer_crm_create_deferred conversation=%s error=%s", conversation.id, exc)

    prompt = _prompt_for_step(state, collected)
    _send_text(db, agent=closer, conversation=conversation, text=prompt)
    _record_activity(
        db,
        conversation=conversation,
        agent=closer,
        outcome="FECHAMENTO_INICIADO",
        description=f"Handoff SDR -> Vendedor IA. {state.handoff_summary}",
    )
    write_audit(
        db,
        user=closer.user,
        action="AI_CLOSER_STARTED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "sdr_agent_id": sdr_agent.id,
            "opportunity_id": state.opportunity_id,
            "plan": state.plan_label,
        },
    )
    return True


def _finish_run(
    db: Session,
    *,
    run: AIRun,
    job: AIJob,
    state: AIClosingState,
    reply: str,
    output: dict[str, Any],
    status: str = "CONCLUIDO",
) -> None:
    run.status = status
    run.intent = "CLOSING"
    run.qualification_score = 100
    run.temperature = "QUENTE"
    run.reply_text = reply
    run.summary = f"Vendedor IA · etapa {state.step}"
    run.output_json = json.dumps(output, ensure_ascii=False, default=str)
    run.finished_at = utcnow()
    job.status = "CONCLUIDO" if status == "CONCLUIDO" else "FALHOU"
    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == job.conversation_id)
    )
    if ai_state:
        ai_state.agent_id = state.agent_id
        ai_state.state = "CLOSING" if state.step not in {"COMPLETED", "ESCALATED"} else state.step
        ai_state.last_run_id = run.id
        ai_state.updated_at = utcnow()


def process_seller_job(db: Session, job: AIJob, agent: AIAgentProfile) -> str:
    conversation = db.get(Conversation, job.conversation_id)
    message = db.get(Message, job.message_id)
    state = db.scalar(
        select(AIClosingState).where(AIClosingState.conversation_id == job.conversation_id)
    )
    if not conversation or not message or not state:
        job.status = "IGNORADO"
        job.error = "Estado do fechamento não encontrado."
        return "skipped"
    if state.step in {"COMPLETED", "ESCALATED"}:
        job.status = "IGNORADO"
        return "skipped"

    run = AIRun(
        job_id=job.id,
        agent_id=agent.id,
        conversation_id=job.conversation_id,
        message_id=job.message_id,
        execution_mode="AUTO" if seller_operational() else "SHADOW",
        model=agent.model,
        status="PROCESSANDO",
        input_snapshot_json=json.dumps(
            {"step": state.step, "message": _redact(message.body), "opportunity_id": state.opportunity_id},
            ensure_ascii=False,
        ),
        started_at=utcnow(),
    )
    db.add(run)
    db.flush()

    if not seller_operational():
        _finish_run(
            db, run=run, job=job, state=state, reply="",
            output={"reason": "seller_not_operational"}, status="FALHOU",
        )
        job.error = "Vendedor IA não está operacional."
        return "configuration"

    collected = _load_collected(state)
    text = (message.body or "").strip()
    reply = ""
    output: dict[str, Any] = {"step_before": state.step}
    client = SulnetCRMClient.from_settings()

    try:
        if state.step == "NAME":
            name = _extract_name(text)
            if not name:
                reply = "Para emitir o contrato corretamente, preciso do seu nome completo. Pode me informar, por favor?"
            else:
                collected["name"] = name
                if conversation.contact:
                    conversation.contact.name = name
                if conversation.lead:
                    conversation.lead.contact_name = name
                _save_collected(state, collected)
                _create_crm_if_ready(db, state=state, conversation=conversation)
                state.step = "CPF"
                reply = _prompt_for_step(state, collected)

        elif state.step == "CPF":
            cpf = _extract_cpf(text)
            if not cpf:
                reply = "Preciso do CPF com 11 dígitos para fazer a consulta cadastral. Pode me enviar?"
            else:
                if not state.opportunity_id:
                    _create_crm_if_ready(db, state=state, conversation=conversation)
                if not state.opportunity_id:
                    raise CRMIntegrationError("A oportunidade ainda não pôde ser criada no CRM.")
                client.update_opportunity(state.opportunity_id, {"cpf": cpf})
                credit = client.consult_credit(state.opportunity_id, cpf)
                state.credit_status = str(credit.get("status") or "")
                state.credit_analysis_id = int(credit.get("analysisId") or 0) or None
                collected["creditStatus"] = state.credit_status
                _save_collected(state, collected)
                if credit.get("approved"):
                    state.step = "CEP"
                    reply = _prompt_for_step(state, collected)
                elif credit.get("rejected"):
                    state.step = "CREDIT_REJECTED"
                    reply = _prompt_for_step(state, collected)
                else:
                    state.step = "CREDIT_REVIEW"
                    reply = "Sua consulta ficou em análise. Vou manter a contratação aberta e sigo assim que o CRM liberar o resultado."

        elif state.step == "CREDIT_REVIEW":
            status = client.get_status(state.opportunity_id or 0)
            credit_status = str((status.get("opportunity") or {}).get("creditStatus") or "")
            state.credit_status = credit_status
            n = _norm(credit_status)
            if any(token in n for token in ["aprovado", "pre-aprovado", "liberado"]):
                state.step = "CEP"
                reply = _prompt_for_step(state, collected)
            elif "reprov" in n:
                state.step = "CREDIT_REJECTED"
                reply = _prompt_for_step(state, collected)
            else:
                reply = "A análise cadastral ainda está em andamento. Assim que houver decisão, eu continuo a contratação."

        elif state.step == "CREDIT_REJECTED":
            interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
            if interpreted.human_requested or interpreted.wants_contest or _yes_no(text) == "SIM":
                _handoff_to_human(
                    db, state=state, conversation=conversation, agent=agent,
                    reason="Cliente solicitou análise/contestação após reprovação de crédito.",
                )
                reply = "Certo. Encaminhei seu caso para análise de um atendente, com todo o histórico da contratação."
            else:
                reply = _prompt_for_step(state, collected)

        elif state.step == "CEP":
            cep = _extract_cep(text)
            if not cep:
                reply = "Pode me informar o CEP com 8 dígitos do endereço de instalação?"
            else:
                address = client.lookup_cep(cep)
                collected.update({
                    "cep": address.get("cep") or cep,
                    "street": address.get("street") or "",
                    "neighborhood": address.get("neighborhood") or "",
                    "city": address.get("city") or "",
                    "uf": address.get("uf") or "",
                })
                client.update_opportunity(state.opportunity_id or 0, {
                    "cep": collected["cep"],
                    "street": collected["street"],
                    "neighborhood": collected["neighborhood"],
                    "city": collected["city"],
                    "uf": collected["uf"],
                })
                _save_collected(state, collected)
                state.step = "ADDRESS_NUMBER"
                reply = _prompt_for_step(state, collected)

        elif state.step == "ADDRESS_NUMBER":
            number, complement = _extract_address_number(text)
            if not number:
                reply = "Qual é o número do imóvel? Se for apartamento, bloco, sala ou fundos, pode informar junto."
            else:
                collected["number"] = number
                if complement:
                    collected["complement"] = complement
                client.update_opportunity(state.opportunity_id or 0, {
                    "number": number,
                    "complement": complement,
                })
                _save_collected(state, collected)
                state.step = "PLAN_CONFIRM"
                reply = _prompt_for_step(state, collected)

        elif state.step == "PLAN_CONFIRM":
            answer = _yes_no(text)
            interpreted = SellerInterpretation()
            if answer == "NAO_INFORMADO" or answer == "NAO":
                interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
                if answer == "NAO_INFORMADO":
                    answer = interpreted.confirmation
            if interpreted.human_requested:
                _handoff_to_human(db, state=state, conversation=conversation, agent=agent, reason="Cliente pediu atendente durante confirmação do plano.")
                reply = "Certo. Encaminhei a conversa para um atendente com todo o histórico."
            elif answer == "SIM":
                try:
                    client.update_opportunity(state.opportunity_id or 0, {"planLabel": state.plan_label})
                except CRMIntegrationError:
                    products = client.products(state.plan_label)
                    if len(products) == 1:
                        client.update_opportunity(state.opportunity_id or 0, {"planId": products[0].get("id"), "planLabel": products[0].get("name")})
                        state.plan_label = str(products[0].get("name") or state.plan_label)
                    else:
                        raise
                state.step = "EMAIL"
                reply = _prompt_for_step(state, collected)
            elif answer == "NAO":
                requested = (interpreted.requested_plan or "").strip()
                if requested:
                    products = client.products(requested)
                    if len(products) == 1:
                        state.plan_label = str(products[0].get("name") or requested)
                        client.update_opportunity(state.opportunity_id or 0, {"planId": products[0].get("id"), "planLabel": state.plan_label})
                        state.step = "EMAIL"
                        reply = f"Certo, alterei para {state.plan_label}. " + _prompt_for_step(state, collected)
                    else:
                        reply = "Sem problema. Qual plano você quer contratar? Me diga a velocidade ou o nome do plano."
                else:
                    reply = "Sem problema. Qual plano você quer contratar? Me diga a velocidade ou o nome do plano."
            else:
                reply = _prompt_for_step(state, collected)

        elif state.step == "EMAIL":
            email = _extract_email(text)
            if not email:
                reply = "Preciso de um e-mail válido para concluir o cadastro. Pode me informar?"
            else:
                collected["email"] = email
                client.update_opportunity(state.opportunity_id or 0, {"clientEmail": email})
                _save_collected(state, collected)
                state.step = "BILLING"
                reply = _prompt_for_step(state, collected)

        elif state.step == "BILLING":
            billing = _billing_local(text)
            if not billing.get("paymentMethod") or not billing.get("dueDay"):
                interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
                if not billing.get("paymentMethod") and interpreted.payment_method:
                    billing["paymentMethod"] = interpreted.payment_method
                if not billing.get("dueDay") and interpreted.due_day:
                    billing["dueDay"] = interpreted.due_day
                if not billing.get("invoiceDelivery") and interpreted.invoice_delivery:
                    billing["invoiceDelivery"] = interpreted.invoice_delivery
            if not billing.get("paymentMethod") and not billing.get("dueDay"):
                reply = "Me informe a forma de pagamento/faturamento e o dia de vencimento. Ex.: boleto, dia 10."
            elif not billing.get("paymentMethod"):
                collected["dueDay"] = billing["dueDay"]
                _save_collected(state, collected)
                reply = "Perfeito. E qual forma de pagamento/faturamento você prefere?"
            elif not billing.get("dueDay"):
                collected["paymentMethod"] = billing["paymentMethod"]
                if billing.get("invoiceDelivery"):
                    collected["invoiceDelivery"] = billing["invoiceDelivery"]
                _save_collected(state, collected)
                reply = "Certo. Qual dia de vencimento você prefere?"
            else:
                collected.update(billing)
                client.update_opportunity(state.opportunity_id or 0, billing)
                _save_collected(state, collected)
                won = client.mark_won(state.opportunity_id or 0)
                contract = won.get("contract") or {}
                state.contract_status = str(contract.get("status") or "aguardando_backoffice")
                state.step = "WAITING_CONTRACT"
                try:
                    started = client.start_contract(state.opportunity_id or 0)
                except CRMIntegrationError:
                    started = contract
                sign_url = str(started.get("signUrl") or "").strip()
                state.contract_status = str(started.get("status") or state.contract_status)
                if sign_url:
                    state.sign_url = sign_url
                    state.step = "WAITING_SIGNATURE"
                    reply = (
                        "Seu cadastro foi concluído e o contrato já está pronto para assinatura. "
                        f"Você pode assinar por este link: {sign_url}"
                    )
                else:
                    reply = _prompt_for_step(state, collected)

        elif state.step in {"WAITING_CONTRACT", "WAITING_SIGNATURE"}:
            status = client.get_status(state.opportunity_id or 0)
            contract = status.get("contract") or {}
            contract_status = str(contract.get("status") or "")
            sign_url = str(contract.get("signUrl") or state.sign_url or "").strip()
            state.contract_status = contract_status
            if contract_status == "assinado":
                state.step = "COMPLETED"
                state.completed_at = utcnow()
                reply = (
                    "Contrato assinado com sucesso! Sua contratação está concluída. "
                    "A ativação segue para a operação da Sulnet, com previsão de até 48 horas."
                )
                if conversation.lead:
                    conversation.lead.stage = "GANHO"
                    conversation.lead.next_action_at = None
            elif sign_url:
                state.sign_url = sign_url
                state.step = "WAITING_SIGNATURE"
                reply = f"Seu contrato está disponível para assinatura aqui: {sign_url}"
            else:
                try:
                    started = client.start_contract(state.opportunity_id or 0)
                except CRMIntegrationError:
                    started = contract
                sign_url = str(started.get("signUrl") or "").strip()
                if sign_url:
                    state.sign_url = sign_url
                    state.step = "WAITING_SIGNATURE"
                    reply = f"Seu contrato ficou pronto. Você pode assinar por este link: {sign_url}"
                else:
                    state.step = "WAITING_CONTRACT"
                    reply = _prompt_for_step(state, collected)

        else:
            reply = "Vou conferir o andamento da sua contratação."
            state.step = "WAITING_CONTRACT"

        _save_collected(state, collected)
        if reply:
            _send_text(db, agent=agent, conversation=conversation, text=reply)
        _record_activity(
            db,
            conversation=conversation,
            agent=agent,
            outcome=state.step,
            description=f"Vendedor IA avançou para {state.step}. Protocolo CRM: {state.protocol or '-'}",
        )
        output.update({
            "step_after": state.step,
            "opportunity_id": state.opportunity_id,
            "protocol": state.protocol,
            "credit_status": state.credit_status,
            "contract_status": state.contract_status,
        })
        _finish_run(db, run=run, job=job, state=state, reply=reply, output=output)
        return "completed"
    except CRMIntegrationError as exc:
        state.last_error = str(exc)
        reply = (
            "Tive uma indisponibilidade ao registrar esta etapa no sistema. "
            "Se ela persistir, seu atendimento será encaminhado sem você precisar repetir os dados."
        )
        try:
            _send_text(db, agent=agent, conversation=conversation, text=reply)
        except Exception:
            LOG.exception("seller_error_reply_failed conversation=%s", conversation.id)
        run.status = "FALHOU"
        run.error = str(exc)[:1200]
        run.reply_text = reply
        run.finished_at = utcnow()
        job.error = str(exc)[:1200]
        if int(job.attempts or 0) < 3:
            job.status = "PENDENTE"
            job.available_at = utcnow() + timedelta(seconds=30 * max(1, int(job.attempts or 1)))
        else:
            _handoff_to_human(
                db, state=state, conversation=conversation, agent=agent,
                reason=f"Falha repetida na integração CRM: {exc}",
            )
            job.status = "FALHOU"
        return "retry"
    except Exception as exc:
        LOG.exception("seller_job_failed job=%s", job.id)
        state.last_error = str(exc)[:1200]
        run.status = "FALHOU"
        run.error = str(exc)[:1200]
        run.finished_at = utcnow()
        job.status = "FALHOU"
        job.error = str(exc)[:1200]
        _handoff_to_human(
            db, state=state, conversation=conversation, agent=agent,
            reason="Falha inesperada no Vendedor IA.",
        )
        return "failed"


def poll_pending_contracts(db: Session, *, limit: int = 10) -> int:
    if not seller_operational():
        return 0
    states = list(
        db.scalars(
            select(AIClosingState).where(
                AIClosingState.step.in_(["WAITING_CONTRACT", "WAITING_SIGNATURE"]),
                AIClosingState.updated_at <= utcnow() - timedelta(seconds=45),
            ).order_by(AIClosingState.updated_at.asc()).limit(limit)
        )
    )
    processed = 0
    client = SulnetCRMClient.from_settings()
    for state in states:
        if not state.opportunity_id:
            continue
        conversation = db.get(Conversation, state.conversation_id)
        agent = db.get(AIAgentProfile, state.agent_id)
        if not conversation or not agent or not agent.active:
            continue
        try:
            status = client.get_status(state.opportunity_id)
            contract = status.get("contract") or {}
            contract_status = str(contract.get("status") or "")
            sign_url = str(contract.get("signUrl") or "").strip()
            state.contract_status = contract_status
            if contract_status == "assinado":
                state.step = "COMPLETED"
                state.completed_at = utcnow()
                _send_text(
                    db,
                    agent=agent,
                    conversation=conversation,
                    text=(
                        "Contrato assinado com sucesso! Sua contratação está concluída. "
                        "A ativação segue para a operação da Sulnet, com previsão de até 48 horas."
                    ),
                )
                if conversation.lead:
                    conversation.lead.stage = "GANHO"
                    conversation.lead.next_action_at = None
                processed += 1
                continue
            if not sign_url and state.step == "WAITING_CONTRACT":
                try:
                    started = client.start_contract(state.opportunity_id)
                    sign_url = str(started.get("signUrl") or "").strip()
                    contract_status = str(started.get("status") or contract_status)
                    state.contract_status = contract_status
                except CRMIntegrationError:
                    pass
            if sign_url and sign_url != state.sign_url:
                state.sign_url = sign_url
                state.step = "WAITING_SIGNATURE"
                _send_text(
                    db,
                    agent=agent,
                    conversation=conversation,
                    text=f"Seu contrato ficou pronto. Você pode assinar por este link: {sign_url}",
                )
                processed += 1
            else:
                state.updated_at = utcnow()
        except CRMIntegrationError as exc:
            state.last_error = str(exc)[:1200]
            state.updated_at = utcnow()
            LOG.warning("seller_contract_poll_failed state=%s error=%s", state.id, exc)
    return processed
