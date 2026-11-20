"""Condições aprovadas para os disparos de fibra 650 Mega de novembro."""
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIReplyDelivery, AIRun, CampaignDispatchRecipient, Message, MessageEvent
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
                if json.loads(snapshot or "{}").get("dispatch_offer_id") != recipient.dispatch_id:
                    return None
    return recipient.dispatch_id if recipient and recipient.dispatch_id in DISPATCH_IDS else None


def offer_decision(context: dict, dispatch_id: int):
    latest = str(context.get("latest_customer_message") or "")
    reply = fold(latest)
    previous = next((row.get("body", "") for row in reversed(context.get("history") or [])
                     if row.get("direction") == "OUTBOUND"), "")
    price_question = any(term in reply for term in ("quanto custa", "qual o valor", "qual valor",
                                                     "qual o preco", "depois dos", "qual a mensalidade"))
    city_reply = _service_city_in_text(latest) if previous.endswith(ASK_CITY) else ""
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
        elif price_question or city:
            decision.reply_text = f"Após as três mensalidades grátis, o valor é R$ {price} por mês com fidelidade. " + (CONFIRM if city else ASK_CITY)
        else:
            decision.reply_text = ASK_CITY
        decision.quick_replies = []
    return decision
