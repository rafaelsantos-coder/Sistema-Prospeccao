"""Entrega de respostas reativas na Fila IA."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIReplyDelivery,
    Conversation,
    Message,
    MessageEvent,
)
from app.services.auto_reply import is_automatic_inbound
from app.services.zapi import ZApiBlacklistedRecipient, ZApiError, ZApiNotConfigured
from app.timeutils import utc_naive, utcnow

LOG = logging.getLogger("sulnet_ai_reply_delivery")

DELIVERED_STATUSES = frozenset({"DELIVERED", "RECEIVED", "READ", "PLAYED"})
FAILED_STATUSES = frozenset({"FAILED", "ERROR", "NOT_SENT", "UNDELIVERED"})
MAX_ATTEMPTS = 3
TEXT_RESCUE_MARKER = "TEXT_RESCUE_V1"
TEXT_RESCUE_WINDOW = timedelta(hours=12)
LOCK_TIMEOUT = timedelta(minutes=3)
ALERT_AFTER = timedelta(minutes=10)
UNSUPPORTED_LINK = frozenset({400, 404, 405, 415, 422})
REJECTED_REQUEST = UNSUPPORTED_LINK | {401, 403, 429}


def queue_ai_reply(
    db: Session,
    *,
    conversation: Conversation,
    agent: AIAgentProfile,
    inbound_id: int,
    text: str,
    buttons: list[dict[str, str]] | None = None,
    link_url: str = "",
    link_label: str = "",
    allow_resolved: bool = False,
) -> Message:
    from app.services.conversation_engine import register_message
    from app.services.ai_queue_scope import is_general_commercial_queue
    from app.services.queue_routing import queue_for_conversation

    if is_general_commercial_queue(queue_for_conversation(db, conversation.id)):
        raise ValueError("Geral Comercial e humana: transfira manualmente para Fila IA.")
    previous = db.scalar(
        select(Message)
        .join(AIReplyDelivery, AIReplyDelivery.outbound_message_id == Message.id)
        .where(AIReplyDelivery.inbound_message_id == inbound_id)
    )
    if previous is not None:
        return previous
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="BUTTONS" if buttons or link_url else "TEXT",
        body=text,
        sender_user_id=agent.user_id,
        status="PENDING",
    )
    message.sent_at = None
    db.flush()
    db.add(AIReplyDelivery(
        inbound_message_id=inbound_id,
        outbound_message_id=message.id,
        agent_id=agent.id,
        buttons_json=json.dumps(buttons or [], ensure_ascii=False),
        link_url=link_url,
        link_label=link_label,
        allow_resolved=allow_resolved or bool(link_url),
    ))
    db.flush()
    LOG.info("ai_reply_queued conversation=%s inbound=%s message=%s", conversation.id, inbound_id, message.id)
    return message


def _can_deliver(db: Session, delivery: AIReplyDelivery, conversation: Conversation) -> bool:
    from app.services.ai_queue_scope import ai_allowed_for_conversation
    from app.services.ai_sdr import human_is_attending

    if conversation.status in {"BLOQUEADO", "ENCERRADO"}:
        return False
    if conversation.status == "RESOLVIDO" and not delivery.allow_resolved:
        return False
    if conversation.lead and (conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR"):
        return False
    if not ai_allowed_for_conversation(db, conversation.id) or human_is_attending(db, conversation):
        return False
    state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
    )
    # O redirecionamento à Central encerra a conversa ao gravar a resposta.
    allowed_states = {"AI_ACTIVE", "CLOSING", "COMPLETED"} if delivery.allow_resolved else {"AI_ACTIVE", "CLOSING"}
    if state and (state.state not in allowed_states or state.agent_id != delivery.agent_id):
        return False
    latest = db.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation.id, Message.direction == "INBOUND")
        .order_by(Message.id.desc())
        .limit(1)
    )
    source = db.get(Message, delivery.inbound_message_id)
    return latest == delivery.inbound_message_id and bool(source and not is_automatic_inbound(source))


def _result_ids(result: dict) -> tuple[str | None, str | None]:
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    return provider_id, zaap_id


def _button_fallback_text(message: Message, buttons: list) -> str:
    labels = [
        str(item.get("label") or "").strip()
        for item in buttons if isinstance(item, dict)
    ]
    labels = [label for label in labels if label]
    if not labels:
        return message.body
    return message.body + "\n\nResponda com uma das opcoes: \n" + "\n".join(
        f"{index}. {label}" for index, label in enumerate(labels, start=1)
    )


def _send(delivery: AIReplyDelivery, message: Message, zapi, phone: str) -> dict:
    if delivery.link_url:
        try:
            return zapi.send_link_button(
                phone=phone, message=message.body, url=delivery.link_url, label=delivery.link_label,
            )
        except ZApiError as exc:
            if exc.status_code not in UNSUPPORTED_LINK:
                raise
            return zapi.send_text(phone=phone, message=f"{message.body}\n{delivery.link_url}")
    buttons = json.loads(delivery.buttons_json)
    if buttons:
        if delivery.link_label == TEXT_RESCUE_MARKER:
            LOG.warning("ai_reply_exhausted_buttons_rescue message=%s", message.id)
            result = zapi.send_text(
                phone=phone, message=_button_fallback_text(message, buttons),
            )
            message.message_type = "TEXT"
            return result
        try:
            return zapi.send_button_list(phone=phone, message=message.body, buttons=buttons)
        except ZApiError as exc:
            # Quando a Z-API rejeita botoes, entregar o conteudo em texto.
            # Erros de acesso, limites e resposta incerta continuam protegidos.
            if exc.status_code not in UNSUPPORTED_LINK:
                raise
            LOG.warning(
                "ai_reply_buttons_fallback_to_text message=%s status=%s",
                message.id, exc.status_code,
            )
            result = zapi.send_text(
                phone=phone, message=_button_fallback_text(message, buttons),
            )
            message.message_type = "TEXT"
            return result
    return zapi.send_text(phone=phone, message=message.body)


def _reconcile_status(db: Session, message: Message) -> None:
    from app.services.conversation_engine import apply_provider_message_status

    ids = [value for value in (message.provider_message_id, message.provider_zaap_id) if value]
    if not ids:
        return
    for event in db.scalars(
        select(MessageEvent).where(
            MessageEvent.provider_message_id.in_(ids),
            MessageEvent.event_type == "STATUS_UNMATCHED",
        ).order_by(MessageEvent.id)
    ):
        apply_provider_message_status(message, event.status or "", event.created_at)
        event.message_id = message.id
        event.event_type = "STATUS"


def _receive_send_result(
    db: Session, delivery: AIReplyDelivery, message: Message, result: dict,
) -> Message:
    provider_id, zaap_id = _result_ids(result)
    if provider_id:
        echoed = db.scalar(
            select(Message).where(Message.provider_message_id == provider_id).with_for_update()
        )
        if echoed and echoed.id != message.id:
            if echoed.direction != "OUTBOUND" or echoed.conversation_id != message.conversation_id:
                message.status = "UNKNOWN"
                return message
            # O callback fromMe pode chegar antes da resposta HTTP da Z-API.
            # Reaproveita o registro já confirmado e remove só a linha de fila criada aqui.
            delivery.outbound_message_id = echoed.id
            echoed.sender_user_id = message.sender_user_id
            db.flush()
            db.delete(message)
            message = echoed
    message.provider_message_id = provider_id
    message.provider_zaap_id = zaap_id or message.provider_zaap_id
    message.sent_at = message.sent_at or utcnow()
    if message.status == "SENDING":
        message.status = "SENT" if provider_id or zaap_id else "UNKNOWN"
    _reconcile_status(db, message)
    return message


def reply_not_sent(db: Session, inbound_id: int) -> bool:
    return bool(db.scalar(
        select(AIReplyDelivery.id)
        .join(Message, Message.id == AIReplyDelivery.outbound_message_id)
        .where(
            AIReplyDelivery.inbound_message_id == inbound_id,
            Message.delivered_at.is_(None),
            Message.status.in_(FAILED_STATUSES | {"PENDING", "SENDING", "UNKNOWN", "CANCELED"}),
        )
    ))


def process_next_ai_reply(db: Session) -> bool:
    """Confirma a posse da tentativa antes de pedir um envio à Z-API."""
    from app.services.audit import write_audit
    from app.services.zapi_instances import service_for_conversation

    now = utcnow()
    work = db.execute(
        select(AIReplyDelivery, Message)
        .join(Message, Message.id == AIReplyDelivery.outbound_message_id)
        .where(
            or_(
                (
                    Message.status.in_(FAILED_STATUSES | {"PENDING"})
                    & (AIReplyDelivery.attempts < MAX_ATTEMPTS)
                    & (AIReplyDelivery.available_at <= now)
                    & Message.delivered_at.is_(None)
                ),
                ((Message.status == "SENDING") & (AIReplyDelivery.locked_at < now - LOCK_TIMEOUT)),
                (
                    Message.status.in_(FAILED_STATUSES)
                    & (AIReplyDelivery.attempts >= MAX_ATTEMPTS)
                    & (AIReplyDelivery.link_label == "")
                    & (AIReplyDelivery.link_url == "")
                    & (AIReplyDelivery.buttons_json != "[]")
                    & (Message.provider_message_id.is_(None))
                    & (Message.provider_zaap_id.is_(None))
                    & (Message.sent_at.is_(None))
                    & (Message.delivered_at.is_(None))
                    & (Message.created_at >= now - TEXT_RESCUE_WINDOW)
                    & (AIReplyDelivery.available_at <= now)
                ),
            )
        )
        .order_by(AIReplyDelivery.available_at, AIReplyDelivery.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if not work:
        return False
    delivery, message = work
    if message.status == "SENDING":
        message.status = "UNKNOWN"
        delivery.locked_at = None
        db.commit()
        LOG.warning("ai_reply_confirmation_missing message=%s", message.id)
        return True

    conversation = db.get(Conversation, message.conversation_id)
    agent = db.get(AIAgentProfile, delivery.agent_id)
    if not conversation or not agent or not agent.active or not _can_deliver(db, delivery, conversation):
        message.status = "CANCELED"
        db.commit()
        return True

    if (
        message.status in FAILED_STATUSES
        and delivery.attempts >= MAX_ATTEMPTS
        and delivery.link_label == ""
    ):
        # Rescue de uma resposta rejeitada, sem ID do provedor. O marcador
        # persiste antes do envio: no maximo uma tentativa extra em texto.
        delivery.link_label = TEXT_RESCUE_MARKER
        LOG.warning(
            "ai_reply_exhausted_rescue_scheduled conversation=%s message=%s",
            conversation.id, message.id,
        )

    message.status = "SENDING"
    message.provider_message_id = None
    message.provider_zaap_id = None
    delivery.attempts += 1
    delivery.locked_at = now
    delivery.available_at = now + timedelta(seconds=30 * delivery.attempts)
    db.commit()
    db.expire_all()

    # A posse ficou registrada. Uma queda depois desta linha exige confirmação
    # do envio para evitar uma segunda mensagem quando o provedor aceitou a primeira.
    phone = (conversation.contact.phone if conversation.contact else "").strip()
    known_rejection = False
    try:
        if not _can_deliver(db, delivery, conversation):
            message.status = "CANCELED"
        else:
            result = _send(delivery, message, service_for_conversation(db, conversation.id), phone)
            message = _receive_send_result(db, delivery, message, result)
    except ZApiBlacklistedRecipient:
        message.status = "CANCELED"
    except ZApiError as exc:
        known_rejection = (
            exc.status_code in REJECTED_REQUEST
            or isinstance(exc, ZApiNotConfigured)
            or isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))
        )
        message.status = "FAILED" if known_rejection else "UNKNOWN"
    except (httpx.HTTPError, ValueError):
        message.status = "UNKNOWN"

    delivery.locked_at = None
    if message.status == "SENT" or message.status in DELIVERED_STATUSES:
        job_id = db.scalar(select(AIJob.id).where(AIJob.message_id == delivery.inbound_message_id))
        write_audit(
            db,
            user=agent.user,
            action="AI_REPLY_SENT",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={"ai_job_id": job_id, "message_id": message.id, "attempt": delivery.attempts},
        )
    db.commit()
    LOG.info(
        "ai_reply_delivery conversation=%s message=%s status=%s attempt=%s rejected=%s",
        conversation.id, message.id, message.status, delivery.attempts, known_rejection,
    )
    return True


def reply_statuses(db: Session, conversation_ids: list[int]) -> dict[int, dict]:
    """Última entrada e estado da resposta, inclusive para conversas anteriores à migração."""
    if not conversation_ids:
        return {}
    inbound_ids = (
        select(func.max(Message.id).label("id"))
        .where(Message.conversation_id.in_(conversation_ids), Message.direction == "INBOUND")
        .group_by(Message.conversation_id)
    )
    inbound = list(db.scalars(select(Message).where(Message.id.in_(inbound_ids))))
    if not inbound:
        return {}
    latest_ids = (
        select(func.max(Message.id).label("id"))
        .where(Message.conversation_id.in_(conversation_ids), Message.direction == "OUTBOUND")
        .group_by(Message.conversation_id)
    )
    outbound = {
        message.conversation_id: message
        for message in db.scalars(select(Message).where(Message.id.in_(latest_ids)))
    }
    inbound_message_ids = [message.id for message in inbound]
    linked = dict(db.execute(
        select(AIReplyDelivery.inbound_message_id, Message)
        .join(Message, Message.id == AIReplyDelivery.outbound_message_id)
        .where(AIReplyDelivery.inbound_message_id.in_(inbound_message_ids))
    ).all())
    jobs = dict(db.execute(
        select(AIJob.message_id, AIJob.status)
        .where(AIJob.message_id.in_(inbound_message_ids))
    ).all())
    now = utcnow()
    statuses = {}
    for source in inbound:
        response = linked.get(source.id) or outbound.get(source.conversation_id)
        if is_automatic_inbound(source):
            label, code = "Aguardando pessoa", "human"
        elif not response or response.id < source.id or response.status == "CANCELED":
            label, code = (
                ("Falha", "failed") if jobs.get(source.id) == "FALHOU" else ("Aguardando IA", "waiting")
            )
        elif response.delivered_at or response.read_at or response.status in DELIVERED_STATUSES:
            label, code = "Entregue", "delivered"
        elif response.status in FAILED_STATUSES | {"UNKNOWN"}:
            label, code = "Falha", "failed"
        elif response.status in {"PENDING", "SENDING"}:
            label, code = "Aguardando IA", "waiting"
        else:
            label, code = "Enviada", "sent"
        age = max(timedelta(), now - utc_naive(source.created_at))
        statuses[source.conversation_id] = {
            "label": label,
            "code": code,
            "attention": code == "failed" or (code == "waiting" and age >= ALERT_AFTER),
            "inbound_id": source.id,
            "age_minutes": int(age.total_seconds() // 60),
            "outbound_id": response.id if response and response.id > source.id else None,
            "outbound_status": response.status if response and response.id > source.id else None,
        }
    return statuses
