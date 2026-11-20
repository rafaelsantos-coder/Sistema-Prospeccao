"""Condições aprovadas para os disparos de fibra 650 Mega de novembro."""
import json
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIReplyDelivery, AIRun, AuditLog, CampaignDispatchRecipient, Message, MessageEvent
from app.services.ai_campaign_recall import fold
from app.services.ai_sdr_policies import (
    _fast_purchase_decision, _ftth_plan_by_speed, _known_city_for_fast_path,
    _plan_available_in_city, _service_city_in_text,
)

DISPATCH_IDS = frozenset({29, 30, 31})
OFFER = "fibra de 650 Mega com três mensalidades grátis"
MARKER = "[Oferta 650/3 · disparo #"
ASK_CITY = f"Para verificar a {OFFER}, em qual cidade será a instalação?"
CONFIRM = f"Podemos seguir com a contratação da {OFFER}?"
INTEREST = {"sim", "s", "ok", "tenho interesse", "eu tenho interesse", "quero", "quero sim",
            "me interessa", "quero saber mais", "tenho interesse sim", "quero contratar",
            "sim quero contratar", "sim tenho interesse", "quero fechar", "vamos fechar", "pode seguir", "pode ser"}


# Identificadores internos gravados somente pelo caminho de oferta atribuída.
PILOT_CITY = "SDR_DISPARO_650_CIDADE"
PILOT_QUOTE = "SDR_DISPARO_650_PROPOSTA"
PILOT_DELAYS = {PILOT_CITY: timedelta(minutes=20), PILOT_QUOTE: timedelta(hours=3)}


def pilot_followup_theme(reply_text: str) -> str | None:
    if reply_text.endswith(ASK_CITY):
        return PILOT_CITY
    if reply_text.endswith(CONFIRM):
        return PILOT_QUOTE
    return None


def pilot_followup_text(theme: str) -> str:
    if theme == PILOT_CITY:
        return ("Para conferir a cobertura dos 650 Mega, pode me mandar a localização pelo "
                "WhatsApp ou me dizer em qual cidade será a instalação. Qual fica mais fácil?")
    if theme == PILOT_QUOTE:
        return ("Sobre os 650 Mega com três mensalidades grátis, posso conferir a cobertura "
                "no seu endereço ou esclarecer como fica a cobrança depois da promoção. "
                "Qual dessas informações te ajuda a decidir?")
    raise ValueError("Tema de retomada desconhecido")


def _reminder_dispatch(db: Session, message: Message) -> int | None:
    audit = db.scalar(select(AuditLog.payload_json).where(
        AuditLog.action == "DISPATCH_PILOT_REMINDER", AuditLog.entity_type == "message",
        AuditLog.entity_id == str(message.id),
    ).limit(1))
    if not audit:
        return None
    original = json.loads(audit).get("source_message_id")
    snapshot = db.scalar(select(AIRun.input_snapshot_json).where(
        AIRun.message_id == original, AIRun.conversation_id == message.conversation_id,
        AIRun.status == "CONCLUIDO",
    ).order_by(AIRun.id.desc()).limit(1))
    return json.loads(snapshot or "{}").get("dispatch_offer_id")


def source_dispatch(db: Session, inbound: Message) -> int | None:
    if inbound.direction != "INBOUND":
        return None
    sent = select(CampaignDispatchRecipient).where(
        CampaignDispatchRecipient.conversation_id == inbound.conversation_id,
        CampaignDispatchRecipient.status == "ENVIADO",
        CampaignDispatchRecipient.sent_at < inbound.created_at,
    )
    if not db.scalar(sent.where(CampaignDispatchRecipient.dispatch_id.in_(DISPATCH_IDS))
                     .with_only_columns(CampaignDispatchRecipient.id).limit(1)):
        return None
    raw = db.scalar(select(MessageEvent.payload_json).where(
        MessageEvent.message_id == inbound.id, MessageEvent.event_type == "RECEIVED_CALLBACK"
    ).limit(1))
    reference = json.loads(raw or "{}").get("referenceMessageId")
    if isinstance(reference, str) and reference:
        recipient = db.scalar(sent.where(CampaignDispatchRecipient.provider_message_id == reference))
        if not recipient:
            reminder = db.scalar(select(Message).where(
                Message.conversation_id == inbound.conversation_id, Message.direction == "OUTBOUND",
                Message.provider_message_id == reference, Message.id < inbound.id,
            ).limit(1))
            attributed = _reminder_dispatch(db, reminder) if reminder else None
            return attributed if attributed in DISPATCH_IDS else None
    else:
        recipient = db.scalar(sent.order_by(CampaignDispatchRecipient.sent_at.desc(),
                                            CampaignDispatchRecipient.id.desc()).limit(1))
        if recipient and recipient.dispatch_id in DISPATCH_IDS:
            # Uma pergunta posterior do vendedor pode ter motivado o "sim".
            last = db.scalar(select(Message).where(
                Message.conversation_id == inbound.conversation_id, Message.id < inbound.id,
                Message.direction == "OUTBOUND", Message.created_at > recipient.sent_at,
            ).order_by(Message.id.desc()).limit(1))
            if last:
                snapshot = db.scalar(select(AIRun.input_snapshot_json).join(
                    AIReplyDelivery, AIReplyDelivery.inbound_message_id == AIRun.message_id
                ).where(AIReplyDelivery.outbound_message_id == last.id,
                        AIRun.status == "CONCLUIDO").order_by(AIRun.id.desc()).limit(1))
                attributed = json.loads(snapshot or "{}").get("dispatch_offer_id") or _reminder_dispatch(db, last)
                if attributed != recipient.dispatch_id:
                    return None
    return recipient.dispatch_id if recipient and recipient.dispatch_id in DISPATCH_IDS else None


def offer_decision(context: dict, dispatch_id: int):
    latest = str(context.get("latest_customer_message") or "")
    reply = fold(latest)
    previous = next((row.get("body", "") for row in reversed(context.get("history") or [])
                     if row.get("direction") == "OUTBOUND"), "")
    price_question = any(term in reply for term in ("quanto custa", "qual o valor", "qual valor",
                                                     "qual o preco", "depois dos", "qual a mensalidade",
                                                     "como funciona a promocao", "como funciona essa promocao",
                                                     "como funcionam os tres meses", "como funcionam esses tres meses",
                                                     "como funcionam os 3 meses", "como funcionam esses 3 meses"))
    if re.search(r"\b(amanha|mais tarde|depois me|me cham[ae]|outro dia|segunda|terca|quarta|quinta|sexta|sabado|domingo)\b", reply):
        return None
    city_reply = _service_city_in_text(latest) if previous.endswith(ASK_CITY) or previous == pilot_followup_text(PILOT_CITY) else ""
    interest_phrase = re.fullmatch(
        r"(?:(?:oi|ola|sim|bom dia|boa tarde|boa noite) )?(?:eu )?(?:tenho interesse|"
        r"quero saber mais|gostaria de saber mais|quero conhecer)(?: (?:na|no|nessa|nesse|sobre a|sobre o)"
        r" (?:oferta|promocao|plano)(?: (?:de )?650 mega)?)?", reply
    )
    if reply not in INTEREST and not interest_phrase and not city_reply and not price_question:
        return None
    plan = _ftth_plan_by_speed(650)
    city = city_reply or _known_city_for_fast_path(context)
    if fold(city) in {"nao informado", "nao informada", "desconhecida"}:
        city = ""
    handoff = bool(city and _plan_available_in_city(plan, city) and previous.endswith(CONFIRM)
                   and reply in {"sim", "ok", "quero", "quero sim", "quero contratar", "quero fechar",
                                 "vamos fechar", "pode seguir", "pode ser", "sim quero contratar"} and not price_question)
    decision = _fast_purchase_decision(plan, city, handoff=handoff)
    decision.summary = f"{MARKER}{dispatch_id}] {OFFER}."
    decision.free_months = 3
    decision.concession_level = 3
    if not handoff:
        price = f"{decision.offer_price:.2f}".replace(".", ",")
        if city and not _plan_available_in_city(plan, city):
            decision.reply_text = f"Preciso confirmar a disponibilidade da {OFFER} para {city}. Qual é o endereço da instalação?"
        else:
            decision.reply_text = f"Após as três mensalidades grátis, o valor é R$ {price} por mês com fidelidade. " + (CONFIRM if city else ASK_CITY)
        decision.quick_replies = []
    return decision
