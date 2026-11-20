"""Contexto de suporte para o WhatsApp da Central e para o histórico do lead."""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIJob,
    AIRun,
    Conversation,
    Message,
)
from app.services.ai_sdr_policy import (
    CENTRAL_BUTTON_LABEL,
    CENTRAL_REDIRECT_MESSAGE,
    CENTRAL_WHATSAPP_URL,
    PLAN_CHANGE_SERVICE_PHONE,
)
from app.services.auto_reply import is_business_auto_reply
from app.timeutils import utcnow

_ISSUES = (
    ("Sem conexão", r"\b(?:sem (?:internet|conexao|sinal)|internet (?:caiu|parou)|internet nao (?:funciona|pega|esta funcionando))\b"),
    ("Quedas na conexão", r"\b(?:quedas?|caindo|cai toda hora|cai muito|oscilando|vai e volta)\b"),
    ("Conexão lenta", r"\b(?:lentidao|lenta|lento|abaixo (?:do|da) contratad[oa]|travando)\b"),
    (
        "Problema na conexão",
        r"\b(?:problemas? (?:na|com a|com minha|no|com o) (?:minha )?(?:internet|conexao|wifi|wi fi|roteador)|"
        r"suporte tecnico|preciso de suporte|falar com (?:o )?suporte)\b",
    ),
)
_TESTS = (
    ("reiniciei o roteador", r"\b(?:reiniciei|reiniciamos|desliguei|desligamos|tirei da tomada)\b.{0,35}\b(?:roteador|modem)\b"),
    ("testei por cabo", r"\b(?:testei|testamos)\b.{0,25}\b(?:cabo|ethernet)\b"),
    ("troquei o cabo", r"\b(?:troquei|trocamos)\b.{0,20}\b(?:cabo|ethernet)\b"),
    ("fiz teste de velocidade", r"\b(?:fiz|fizemos|rodei|rodamos)\b.{0,30}\b(?:teste de velocidade|speedtest)\b"),
    (
        "já recebi uma visita técnica",
        r"\b(?:(?:recebi|recebemos|ja tive|ja tivemos|ja houve)\b.{0,28}\bvisita tecnica|"
        r"(?:o )?tecnico (?:ja )?(?:veio|passou)|(?:ja )?veio (?:o |um )?tecnico)\b",
    ),
)
_CURRENT_PLAN = re.compile(
    r"\b(?:(?:meu plano|minha internet)(?: atual)? (?:e |tem )?(?:de )?|"
    r"(?:tenho|assino) (?:o |um )?plano (?:de )?)(\d{1,4})\s*(mega(?:s)?|mbps|mb|gb)\b",
)
_PRICE_REVIEW = re.compile(
    r"\b(?:diminuir|baixar|reduzir|revisar|rever|ajustar|negociar|redu[cç]ao|revisao|desconto)\b"
    r".{0,38}\b(?:valor|preco|mensalidade|fatura|o que pago|plano atual)\b",
)
_NEW_CONNECTION = re.compile(
    r"\b(?:(?:nova|outra|segunda) conexao|(?:internet|fibra|conexao) (?:pra|para|em) (?:um )?(?:outro|novo) endereco)\b",
)
_EXISTING_ACCOUNT_REQUEST = re.compile(
    r"\b(?:financeiro|segunda via|2 via|minha fatura|boleto|mudanca de endereco|"
    r"cancelar (?:meu plano|minha internet)|upgrade ou suporte)\b",
)
_CONTINUES_SUPPORT = re.compile(r"\b(?:continua|continuou|segue|persiste|mesmo problema|nao resolveu)\b")
_NEGATED_ACTION = re.compile(
    r"\b(?:nao|nem|nunca)(?: (?:fiz|recebi|foi|foi feito|consegui|quero|preciso|vou))?(?: uma?)?\s*$",
)


def _said(pattern: str | re.Pattern, text: str) -> bool:
    return any(
        not _NEGATED_ACTION.search(text[max(0, match.start() - 32):match.start()])
        for match in re.finditer(pattern, text)
    )


def service_handoff(context: dict) -> dict:
    """Guarda só fatos confirmados pelo cliente, sem copiar o texto integral."""
    from app.services.ai_sdr_policies import (
        _EXPLICIT_CONTACT_STOP_CUES,
        _existing_customer_from_context,
        _existing_customer_plan_change_requested,
        _fold_sales_text,
        _mentions_competitor_provider,
    )

    latest = _fold_sales_text(context.get("latest_customer_message"))
    status = _existing_customer_from_context(context)
    if not latest or status == "NAO_CLIENTE" or any(cue in latest for cue in _EXPLICIT_CONTACT_STOP_CUES):
        return {}
    # "Minha internet da Vivo está lenta" é uma objeção de venda, não suporte Sulnet.
    if any(
        _mentions_competitor_provider(provider)
        for provider in re.findall(r"\b(?:internet|conexao|plano|sou cliente) (?:da|do) ([a-z]+)", latest)
    ):
        return {}

    rows = [
        (row, _fold_sales_text(row.get("body")))
        for row in (context.get("history") or [])[-30:]
        if str(row.get("direction") or "").upper() == "INBOUND" and not is_business_auto_reply(row.get("body"))
    ]
    latest_tests = any(_said(pattern, latest) for _, pattern in _TESTS)
    latest_is_review = _said(_PRICE_REVIEW, latest)
    new_connection = _said(_NEW_CONNECTION, latest)
    issue = next((label for label, pattern in _ISSUES if _said(pattern, latest)), "")
    if not issue and (_CONTINUES_SUPPORT.search(latest) or latest_tests or latest_is_review):
        for _, body in reversed(rows):
            issue = next((label for label, pattern in _ISSUES if _said(pattern, body)), "")
            if issue:
                break
    if issue and re.search(r"\b(?:como funciona|como e|qual e|tem) (?:o )?suporte tecnico\b", latest):
        issue = ""
    if not issue and not new_connection and _existing_customer_plan_change_requested(context):
        issue = "Alteração do plano atual"
    if not issue and status == "CLIENTE" and latest_is_review:
        issue = "Revisão do valor do plano atual"
    if not issue and status == "CLIENTE" and _said(_EXISTING_ACCOUNT_REQUEST, latest):
        issue = "Atendimento do serviço atual"
    # Problemas genéricos podem ser reconhecidos sem o cliente repetir se já é assinante.
    if not issue:
        return {}

    plan = ""
    for _, body in reversed(rows):
        match = _CURRENT_PLAN.search(body)
        if match:
            unit = "GB" if match[2] == "gb" else "Mega"
            plan = f"{int(match[1])} {unit}"
            break
    tests = [
        label for label, pattern in _TESTS
        if _said(pattern, latest) or any(_said(pattern, body) for _, body in rows)
    ]
    review = latest_is_review or any(_said(_PRICE_REVIEW, body) for _, body in rows)
    if not new_connection:
        for _, body in reversed(rows):
            if _NEW_CONNECTION.search(body):
                new_connection = _said(_NEW_CONNECTION, body)
                break
    return {
        "issue": issue,
        "plan": plan,
        "tests": tests,
        "review_price": review,
        "new_connection": new_connection,
        "source_message_ids": [row["id"] for row, _ in rows if row.get("id")][-10:],
    }


def handoff_summary(handoff: dict) -> str:
    lines = [handoff["issue"] + "."]
    if handoff["plan"]:
        lines.append(f"Plano atual informado de {handoff['plan']}.")
    if handoff["tests"]:
        lines.append("Relato do cliente\n" + "\n".join(f"• {test}" for test in handoff["tests"]))
    if handoff["review_price"]:
        lines.append("O cliente também pediu revisão do valor.")
    if handoff["new_connection"]:
        lines.append("Há um pedido comercial em aberto para uma nova conexão.")
    return "\n".join(lines)


def central_handoff_reply(handoff: dict) -> str:
    if not (handoff["plan"] or handoff["tests"] or handoff["review_price"] or handoff["new_connection"]):
        return CENTRAL_REDIRECT_MESSAGE
    reply = (
        f"A Central Sulnet atende pelo WhatsApp {PLAN_CHANGE_SERVICE_PHONE}. "
        "Toque em Falar com a central para abrir o resumo que você pode enviar à equipe."
    )
    if handoff["review_price"]:
        reply += " Incluí seu pedido de revisão do valor."
    if handoff["new_connection"]:
        reply += " A nova conexão fica registrada para seguirmos por aqui quando você quiser."
    return reply


def central_handoff_url(handoff: dict) -> str:
    if not handoff:
        return CENTRAL_WHATSAPP_URL
    lines = ["Olá! Preciso de ajuda da Central Sulnet.", handoff["issue"] + "."]
    if handoff["plan"]:
        lines.append(f"Meu plano atual é de {handoff['plan']}.")
    if handoff["tests"]:
        lines.extend(f"• {test.capitalize()}" for test in handoff["tests"])
    if handoff["review_price"]:
        lines.append("Também quero revisar o valor do plano atual.")
    query = urlencode({"text": "\n".join(lines)})
    return f"{CENTRAL_WHATSAPP_URL}?{query}"


def record_service_handoff(
    db: Session, conversation: Conversation, message: Message, user_id: int, handoff: dict,
) -> None:
    lead = conversation.lead
    if not lead or not handoff:
        return
    try:
        payload = json.loads(lead.raw_data or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    previous = payload.get("ai_service_handoff")
    if isinstance(previous, dict) and previous.get("inbound_id") == message.id:
        return
    pending = ["SUPORTE_CENTRAL"]
    if handoff["review_price"]:
        pending.append("REVISAO_VALOR")
    if handoff["new_connection"]:
        pending.append("NOVA_CONEXAO")
    payload["ai_service_handoff"] = {
        **handoff, "conversation_id": conversation.id, "inbound_id": message.id,
        "at": message.created_at.isoformat() if message.created_at else None,
        "pending_requests": pending,
    }
    lead.raw_data = json.dumps(payload, ensure_ascii=False)
    db.add(Activity(
        lead_id=lead.id, user_id=user_id, kind="ATENDIMENTO_CENTRAL", status="PENDENTE",
        channel="WHATSAPP", outcome="AGUARDANDO_CENTRAL", description=handoff_summary(handoff),
    ))


def handle_closing_support(db: Session, job: AIJob, agent: AIAgentProfile) -> bool:
    """Encaminha o suporte sem avançar a etapa de uma contratação em curso."""
    from app.services.ai_followups import cancel_pending_followups
    from app.services.ai_reply_delivery import queue_ai_reply
    from app.services.ai_sdr import (
        _confirmed_existing_customer_from_lead,
        _conversation_messages,
        human_is_attending,
    )
    from app.services.ai_sdr_policies import _confirmed_facts_from_history
    from app.services.ai_seller import _finish_run, seller_operational

    if agent.execution_mode != "AUTO" or not seller_operational():
        return False
    owner = db.scalar(select(AIConversationState).where(AIConversationState.conversation_id == job.conversation_id))
    if owner is None or owner.agent_id != agent.id or owner.state != "CLOSING":
        return False
    closing = db.scalar(select(AIClosingState).where(AIClosingState.conversation_id == job.conversation_id))
    if closing is None or closing.step in {"COMPLETED", "PLAN_CHANGE", "ESCALATED"}:
        return False
    conversation = db.get(Conversation, job.conversation_id)
    message = db.get(Message, job.message_id)
    if not conversation or not message or human_is_attending(db, conversation):
        return False
    if conversation.status in {"BLOQUEADO", "ENCERRADO", "RESOLVIDO"} or (
        conversation.lead and (conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR")
    ):
        return False
    history = _conversation_messages(db, conversation.id, limit=30)
    handoff = service_handoff({
        "latest_customer_message": message.body,
        "confirmed_facts": _confirmed_facts_from_history(history),
        "lead": {"confirmed_existing_customer": _confirmed_existing_customer_from_lead(
            conversation.lead, conversation_id=conversation.id,
        )},
        "history": [{"id": row.id, "direction": row.direction, "body": row.body} for row in history],
    })
    if not handoff:
        return False
    reply = central_handoff_reply(handoff)
    with db.begin_nested():
        queue_ai_reply(
            db, conversation=conversation, agent=agent, inbound_id=message.id, text=reply,
            link_url=central_handoff_url(handoff), link_label=CENTRAL_BUTTON_LABEL,
        )
        record_service_handoff(db, conversation, message, agent.user_id, handoff)
        cancel_pending_followups(db, conversation.id, reason="EXISTING_CUSTOMER_PLAN_CHANGE")
        run = AIRun(
            job_id=job.id, agent_id=agent.id, conversation_id=conversation.id, message_id=message.id,
            execution_mode="AUTO", model=agent.model, status="PROCESSANDO", started_at=utcnow(),
        )
        db.add(run)
        db.flush()
        _finish_run(
            db, run=run, job=job, state=closing, reply=reply,
            output={"step_before": closing.step, "step_after": closing.step, "service_handoff": handoff},
        )
    return True
