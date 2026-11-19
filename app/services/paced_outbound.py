"""Controle duravel de primeiras mensagens manuais por numero Z-API.

Uma unica agenda de 30s por instancia, mesmo com varios vendedores e replicas.
Nao segura requests HTTP, nao altera o atendimento em tempo real e nao
reativa campanhas ou IA. Falhas ambiguas nao sao reenviadas automaticamente.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Contact,
    Conversation,
    Message,
    PacedOutboundMessage,
    ZApiInstance,
)
from app.services.conversation_engine import register_message
from app.services.global_blacklist import is_globally_blocked
from app.services.zapi import ZApiBlacklistedRecipient, ZApiError
from app.services.zapi_instances import service_for_conversation
from app.timeutils import utcnow, utc_naive

LOG = logging.getLogger("sulnet_paced_outbound")
PACING_SECONDS = max(30, int(os.getenv("ZAPI_FIRST_CONTACT_INTERVAL_SECONDS", "30")))
PACING_REPLY_HOURS = 24


def is_live_conversation(db: Session, conversation: Conversation) -> bool:
    """A resposta do cliente nas ultimas 24h libera conversacao em tempo real."""
    if conversation.source == "TELEGRAM":
        return True
    return db.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation.id,
            Message.direction == "INBOUND",
            Message.created_at >= utcnow() - timedelta(hours=PACING_REPLY_HOURS),
        ).limit(1)
    ) is not None


def needs_pacing(db: Session, conversation: Conversation) -> bool:
    return not is_live_conversation(db, conversation)


def queue_manual_message(
    db: Session,
    conversation: Conversation,
    *,
    message_type: str,
    body: str,
    sender_user_id: int,
    media_url: str | None = None,
    mime_type: str | None = None,
    caption: str = "",
    filename: str = "",
) -> tuple[Message, PacedOutboundMessage]:
    """Cria mensagem e reserva janela. Chamador deve confirmar a transacao."""
    service = service_for_conversation(db, conversation.id)
    if conversation.source == "TELEGRAM":
        raise ValueError("O pacing nao se aplica ao Telegram.")
    if not service.instance_id:
        raise ZApiError("Instancia WhatsApp indisponivel para agendar mensagem.")
    if is_globally_blocked(db, conversation.contact.phone):
        raise ZApiBlacklistedRecipient("Contato bloqueado pela blacklist global.")

    # Bloquear a linha da instancia serializa agendamentos entre replicas/usuarios.
    instance = db.scalar(
        select(ZApiInstance).where(
            ZApiInstance.instance_id == service.instance_id,
        ).with_for_update()
    )
    if db.bind.dialect.name == "postgresql" and instance is None:
        # Caso a instancia use as variaveis legadas e nao tenha registro.
        db.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext(:key))"
            ), {"key": "paced:" + service.instance_id},
        )

    now = utcnow()
    last_slot = db.scalar(
        select(func.max(PacedOutboundMessage.scheduled_at)).where(
            PacedOutboundMessage.instance_key == service.instance_id,
        )
    )
    base = max(now, utc_naive(last_slot)) if last_slot else now
    scheduled = base + timedelta(seconds=PACING_SECONDS)
    message = register_message(
        db, conversation, direction="OUTBOUND",
        message_type=message_type, body=body,
        sender_user_id=sender_user_id, media_url=media_url,
        mime_type=mime_type, status="PENDING",
    )
    # register_message assumes outbound was sent; restore pending semantics.
    message.sent_at = None
    db.flush()
    item = PacedOutboundMessage(
        message_id=message.id,
        instance_key=service.instance_id,
        scheduled_at=scheduled,
        status="PENDING",
        caption=caption[:1024],
        filename=filename[:240],
    )
    db.add(item)
    db.flush()
    return message, item


def process_one_pending() -> str:
    """Processa uma entrada vencida; erro ambiguo nunca gera reenvio invisivel."""
    with SessionLocal() as db:
        with db.begin():
            candidates = list(db.scalars(
                select(PacedOutboundMessage)
                .where(
                    PacedOutboundMessage.status == "PENDING",
                    PacedOutboundMessage.scheduled_at <= utcnow(),
                )
                .order_by(PacedOutboundMessage.scheduled_at, PacedOutboundMessage.id)
                .with_for_update(skip_locked=True)
                .limit(100)
            ))
            item = None
            for candidate in candidates:
                # A linha da instancia sincroniza todos os workers, inclusive replicas.
                db.scalar(
                    select(ZApiInstance).where(
                        ZApiInstance.instance_id == candidate.instance_key,
                    ).with_for_update()
                )
                if db.bind.dialect.name == "postgresql" and not db.scalar(
                    select(ZApiInstance.id).where(
                        ZApiInstance.instance_id == candidate.instance_key,
                    )
                ):
                    db.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": "paced:" + candidate.instance_key},
                    )
                inflight = db.scalar(
                    select(PacedOutboundMessage.id).where(
                        PacedOutboundMessage.instance_key == candidate.instance_key,
                        PacedOutboundMessage.status == "PROCESSING",
                    ).limit(1)
                )
                if inflight:
                    continue
                last_sent = db.scalar(
                    select(func.max(PacedOutboundMessage.sent_at)).where(
                        PacedOutboundMessage.instance_key == candidate.instance_key,
                        PacedOutboundMessage.status == "SENT",
                    )
                )
                if last_sent and utc_naive(last_sent) + timedelta(seconds=PACING_SECONDS) > utcnow():
                    continue
                item = candidate
                break
            if item is None:
                return "idle"
            # Marca antes da chamada externa: queda da aplicacao nao replica envio.
            item.status = "PROCESSING"
            item.claimed_at = utcnow()
            item_id = item.id

    with SessionLocal() as db:
        item = db.get(PacedOutboundMessage, item_id)
        if not item or item.status != "PROCESSING":
            return "idle"
        msg = db.get(Message, item.message_id)
        conv = db.get(Conversation, msg.conversation_id) if msg else None
        contact = db.get(Contact, msg.contact_id) if msg else None
        if not msg or not conv or not contact:
            item.status = "FAILED"
            item.error = "Mensagem, conversa ou contato removido."
            if msg:
                msg.status = "FAILED"
            db.commit()
            return "failed"
        try:
            if is_globally_blocked(db, contact.phone) or conv.status == "BLOQUEADO":
                raise ZApiBlacklistedRecipient("Contato bloqueado antes do envio.")
            service = service_for_conversation(db, conv.id)
            if service.instance_id != item.instance_key:
                raise ZApiError("Instancia alterada; envio requer revisao manual.")
            # Url de midia precisa ser assinada somente no momento do envio.
            if msg.message_type == "TEXT":
                result = service.send_text(phone=contact.phone, message=msg.body)
            else:
                from app.services.media_storage import MediaStorage
                storage = MediaStorage()
                url = storage.presigned_download_url(
                    msg.media_url or "", filename=item.filename,
                    expires_seconds=3600,
                )
                if msg.message_type == "IMAGE":
                    result = service.send_image(phone=contact.phone, image_url=url, caption=item.caption)
                elif msg.message_type == "VIDEO":
                    result = service.send_video(phone=contact.phone, video_url=url, caption=item.caption)
                elif msg.message_type == "AUDIO":
                    result = service.send_audio(phone=contact.phone, audio_url=url)
                elif msg.message_type == "DOCUMENT":
                    extension = (item.filename.rsplit(".", 1)[-1] if "." in item.filename else "bin")
                    result = service.send_document(
                        phone=contact.phone, document_url=url,
                        extension=extension, file_name=item.filename,
                        caption=item.caption,
                    )
                else:
                    raise ZApiError("Tipo de mensagem nao suportado pelo agendador.")
        except Exception as exc:
            LOG.warning("paced_outbound_failed item=%s type=%s", item_id, type(exc).__name__)
            item.status = "BLOCKED" if isinstance(exc, ZApiBlacklistedRecipient) else "FAILED"
            item.error = str(exc)[:300]
            msg.status = "FAILED"
            db.commit()
            return "failed"

        provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
        zaap_id = str(result.get("zaapId") or "").strip() or None
        msg.provider_message_id = provider_id
        msg.provider_zaap_id = zaap_id
        msg.status = "SENT"
        msg.sent_at = utcnow()
        item.status = "SENT"
        item.sent_at = utcnow()
        if conv.lead:
            conv.lead.last_contact_at = utcnow()
        db.commit()
        LOG.info("paced_outbound_sent id=%s instance_key=%s", item_id, item.instance_key)
        return "sent"


def run_paced_worker() -> None:
    """Worker exclusivo para a outbox manual: nao interfere nas campanhas."""
    LOG.info("Manual WhatsApp pacing worker ativo: intervalo=%ss", PACING_SECONDS)
    while True:
        try:
            result = process_one_pending()
        except Exception:
            LOG.exception("Falha do worker de mensagens manuais")
            result = "failed"
        time.sleep(1 if result == "sent" else 3)
