"""WhatsApp Cloud API oficial da Meta — mesma interface de envio da Z-API.

Cadastro: Phone Number ID + token permanente (ou de sistema) do app Meta.
Cada fila pode usar um número Meta em vez de uma instância Z-API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConversationMetaAccount,
    MetaWhatsAppAccount,
    ServiceQueue,
    ServiceQueueMetaAccount,
    ServiceQueueZApiInstance,
)
from app.services.secret_store import decrypt_secret, encrypt_secret
from app.services.zapi import ZApiBlacklistedRecipient, ZApiError, ZApiNotConfigured

LOG = logging.getLogger("sulnet_meta_whatsapp")

DEFAULT_GRAPH_VERSION = "v21.0"
GRAPH_BASE = "https://graph.facebook.com"
MIN_WEBHOOK_KEY_LENGTH = 32
PROVIDER_META = "META"
PROVIDER_ZAPI = "ZAPI"


def is_official_digital(account: MetaWhatsAppAccount) -> bool:
    return re.sub(r"\D", "", account.phone_label or "") in {"555535118003", "5535118003"} or account.name.strip().casefold() == "sulnet - meta oficial 01"


def is_digital_queue(queue: ServiceQueue) -> bool:
    return queue.team == "DIGITAL" and (
        queue.name.strip().casefold() in {"digital", "fila digital"}
        or queue.code.strip().upper() in {"DIGITAL", "FILA-DIGITAL", "FILA_DIGITAL"}
    )


def meta_linked_queues(db: Session, account_id: int) -> list[ServiceQueue]:
    return list(db.scalars(select(ServiceQueue).join(
        ServiceQueueMetaAccount, ServiceQueueMetaAccount.queue_id == ServiceQueue.id
    ).where(ServiceQueueMetaAccount.meta_account_id == account_id).order_by(ServiceQueue.id)))


class MetaWhatsAppError(ZApiError):
    pass


class MetaWhatsAppNotConfigured(ZApiNotConfigured):
    pass


def list_meta_accounts(db: Session, *, active_only: bool = True) -> list[MetaWhatsAppAccount]:
    stmt = select(MetaWhatsAppAccount)
    if active_only:
        stmt = stmt.where(MetaWhatsAppAccount.active.is_(True))
    return list(db.scalars(stmt.order_by(MetaWhatsAppAccount.name, MetaWhatsAppAccount.id)))


def get_meta_account(db: Session, account_id: int | None) -> MetaWhatsAppAccount | None:
    if not account_id:
        return None
    return db.get(MetaWhatsAppAccount, account_id)


def account_by_webhook_key(db: Session, webhook_key: str) -> MetaWhatsAppAccount | None:
    if not webhook_key or len(webhook_key) < MIN_WEBHOOK_KEY_LENGTH:
        return None
    for account in db.scalars(select(MetaWhatsAppAccount).where(MetaWhatsAppAccount.active.is_(True))):
        if secrets.compare_digest(account.webhook_key or "", webhook_key):
            return account
    return None


def account_by_phone_number_id(
    db: Session,
    phone_number_id: str,
) -> MetaWhatsAppAccount | None:
    clean = (phone_number_id or "").strip()
    if not clean:
        return None
    return db.scalar(
        select(MetaWhatsAppAccount).where(
            MetaWhatsAppAccount.phone_number_id == clean,
            MetaWhatsAppAccount.active.is_(True),
        )
    )


def account_webhook_url(account: MetaWhatsAppAccount) -> str:
    if not settings.app_public_url:
        return ""
    return f"{settings.app_public_url}/webhooks/meta/{account.webhook_key}"


def format_display_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("55") and len(digits) in {12, 13}:
        ddd = digits[2:4]
        local = digits[4:]
        if len(local) == 9:
            return f"+55 ({ddd}) {local[:5]}-{local[5:]}"
        if len(local) == 8:
            return f"+55 ({ddd}) {local[:4]}-{local[4:]}"
    return f"+{digits}" if digits else ""


def create_meta_account(
    db: Session,
    *,
    name: str,
    phone_label: str,
    phone_number_id: str,
    waba_id: str,
    access_token: str,
    app_secret: str,
    verify_token: str,
    graph_api_version: str,
    created_by_id: int | None,
) -> MetaWhatsAppAccount:
    item = MetaWhatsAppAccount(
        name=name.strip(),
        phone_label=phone_label.strip(),
        phone_number_id=phone_number_id.strip(),
        waba_id=waba_id.strip(),
        access_token_encrypted=encrypt_secret(access_token),
        app_secret_encrypted=encrypt_secret(app_secret) if app_secret.strip() else "",
        verify_token=verify_token.strip() or secrets.token_urlsafe(24),
        webhook_key=secrets.token_urlsafe(36),
        graph_api_version=(graph_api_version.strip() or DEFAULT_GRAPH_VERSION),
        active=True,
        created_by_id=created_by_id,
    )
    db.add(item)
    db.flush()
    return item


def queue_meta_account(db: Session, queue_id: int | None) -> MetaWhatsAppAccount | None:
    if not queue_id:
        return None
    return db.scalar(
        select(MetaWhatsAppAccount)
        .join(
            ServiceQueueMetaAccount,
            ServiceQueueMetaAccount.meta_account_id == MetaWhatsAppAccount.id,
        )
        .where(
            ServiceQueueMetaAccount.queue_id == queue_id,
            MetaWhatsAppAccount.active.is_(True),
        )
    )


def clear_queue_meta_binding(db: Session, queue_id: int) -> None:
    db.execute(
        delete(ServiceQueueMetaAccount).where(ServiceQueueMetaAccount.queue_id == queue_id)
    )


def clear_queue_zapi_binding(db: Session, queue_id: int) -> None:
    db.execute(
        delete(ServiceQueueZApiInstance).where(ServiceQueueZApiInstance.queue_id == queue_id)
    )


def bind_queue_meta_account(
    db: Session,
    queue_id: int,
    meta_account_id: int,
) -> ServiceQueueMetaAccount:
    account = db.scalar(select(MetaWhatsAppAccount).where(
        MetaWhatsAppAccount.id == meta_account_id
    ).with_for_update())
    queue = db.get(ServiceQueue, queue_id)
    if not account or not account.active or not queue:
        raise ValueError("Conta Meta ou fila inválida.")
    if is_official_digital(account) and not is_digital_queue(queue):
        raise ValueError("O número oficial 3511-8003 é exclusivo da fila Digital.")
    if any(q.id != queue_id for q in meta_linked_queues(db, account.id)):
        raise ValueError("Número Meta já vinculado a outra fila.")
    clear_queue_zapi_binding(db, queue_id)
    from app.services.sulnet_wa import clear_queue_binding as clear_sulnet_queue_binding
    clear_sulnet_queue_binding(db, queue_id)
    link = db.scalar(
        select(ServiceQueueMetaAccount).where(ServiceQueueMetaAccount.queue_id == queue_id)
    )
    if not link:
        link = ServiceQueueMetaAccount(queue_id=queue_id, meta_account_id=meta_account_id)
        db.add(link)
    else:
        link.meta_account_id = meta_account_id
    db.flush()
    return link


def conversation_meta_account(
    db: Session,
    conversation_id: int | None,
) -> MetaWhatsAppAccount | None:
    if not conversation_id:
        return None
    return db.scalar(
        select(MetaWhatsAppAccount)
        .join(
            ConversationMetaAccount,
            ConversationMetaAccount.meta_account_id == MetaWhatsAppAccount.id,
        )
        .where(
            ConversationMetaAccount.conversation_id == conversation_id,
            MetaWhatsAppAccount.active.is_(True),
        )
    )


def bind_conversation_meta_account(
    db: Session,
    conversation_id: int,
    meta_account_id: int,
) -> ConversationMetaAccount:
    link = db.scalar(
        select(ConversationMetaAccount).where(
            ConversationMetaAccount.conversation_id == conversation_id
        )
    )
    if not link:
        link = ConversationMetaAccount(
            conversation_id=conversation_id,
            meta_account_id=meta_account_id,
        )
        db.add(link)
    else:
        link.meta_account_id = meta_account_id
    db.flush()
    return link


def default_queue_for_meta_account(
    db: Session,
    meta_account_id: int,
) -> ServiceQueue | None:
    account = db.get(MetaWhatsAppAccount, meta_account_id)
    queues = meta_linked_queues(db, meta_account_id)
    if not account or not account.active or len(queues) != 1 or not queues[0].active:
        return None
    queue = queues[0]
    if is_official_digital(account) and not is_digital_queue(queue):
        return None
    return queue


def service_for_meta_account(account: MetaWhatsAppAccount) -> "MetaWhatsAppService":
    return MetaWhatsAppService(
        phone_number_id=account.phone_number_id,
        access_token=decrypt_secret(account.access_token_encrypted),
        graph_api_version=account.graph_api_version or DEFAULT_GRAPH_VERSION,
        app_secret=(
            decrypt_secret(account.app_secret_encrypted)
            if account.app_secret_encrypted
            else ""
        ),
    )


def verify_webhook_signature(*, app_secret: str, body: bytes, signature_header: str) -> bool:
    if not app_secret:
        return True
    expected_prefix = "sha256="
    if not signature_header.startswith(expected_prefix):
        return False
    digest = hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return secrets.compare_digest(signature_header[len(expected_prefix) :], digest)


def meta_inbound_to_engine_payload(message: dict[str, Any], *, contact_wa_id: str) -> dict[str, Any]:
    """Converte um item ``messages[]`` da Cloud API no formato esperado pelo conversation_engine."""
    payload: dict[str, Any] = {
        "phone": contact_wa_id,
        "fromMe": False,
        "messageId": str(message.get("id") or ""),
    }
    msg_type = str(message.get("type") or "")
    if msg_type == "text":
        text = message.get("text") if isinstance(message.get("text"), dict) else {}
        payload["text"] = {"message": str(text.get("body") or "").strip()}
    elif msg_type == "button":
        button = message.get("button") if isinstance(message.get("button"), dict) else {}
        payload["buttonsResponseMessage"] = {
            "message": str(button.get("text") or button.get("payload") or "").strip(),
            "buttonId": str(button.get("payload") or ""),
        }
    elif msg_type == "interactive":
        interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
        itype = str(interactive.get("type") or "")
        if itype == "button_reply":
            reply = interactive.get("button_reply") if isinstance(interactive.get("button_reply"), dict) else {}
            payload["buttonsResponseMessage"] = {
                "message": str(reply.get("title") or "").strip(),
                "buttonId": str(reply.get("id") or ""),
            }
        elif itype == "list_reply":
            reply = interactive.get("list_reply") if isinstance(interactive.get("list_reply"), dict) else {}
            payload["listResponseMessage"] = {
                "message": str(reply.get("title") or "").strip(),
                "selectedRowId": str(reply.get("id") or ""),
                "description": str(reply.get("description") or ""),
            }
        else:
            payload["text"] = {"message": "[Resposta interativa]"}
    elif msg_type == "image":
        image = message.get("image") if isinstance(message.get("image"), dict) else {}
        payload["image"] = {
            "caption": str(image.get("caption") or "[Imagem]"),
            "imageUrl": "",
            "mimeType": str(image.get("mime_type") or ""),
            "meta_media_id": str(image.get("id") or ""),
        }
    elif msg_type == "audio":
        audio = message.get("audio") if isinstance(message.get("audio"), dict) else {}
        payload["audio"] = {
            "audioUrl": "",
            "mimeType": str(audio.get("mime_type") or ""),
            "meta_media_id": str(audio.get("id") or ""),
        }
    elif msg_type == "video":
        video = message.get("video") if isinstance(message.get("video"), dict) else {}
        payload["video"] = {
            "caption": str(video.get("caption") or "[Video]"),
            "videoUrl": "",
            "mimeType": str(video.get("mime_type") or ""),
            "meta_media_id": str(video.get("id") or ""),
        }
    elif msg_type == "document":
        document = message.get("document") if isinstance(message.get("document"), dict) else {}
        payload["document"] = {
            "fileName": str(document.get("filename") or "[Documento]"),
            "documentUrl": "",
            "mimeType": str(document.get("mime_type") or ""),
            "meta_media_id": str(document.get("id") or ""),
        }
    elif msg_type == "location":
        location = message.get("location") if isinstance(message.get("location"), dict) else {}
        payload["location"] = {
            "name": str(location.get("name") or ""),
            "address": str(location.get("address") or ""),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
        }
    elif msg_type == "contacts":
        contacts = message.get("contacts") if isinstance(message.get("contacts"), list) else []
        first = contacts[0] if contacts and isinstance(contacts[0], dict) else {}
        name = first.get("name") if isinstance(first.get("name"), dict) else {}
        payload["contact"] = {
            "displayName": str(name.get("formatted_name") or "[Contato]"),
        }
    else:
        payload["text"] = {"message": f"[Mensagem {msg_type or 'desconhecida'}]"}
    return payload


def meta_status_to_engine_payload(status_item: dict[str, Any]) -> dict[str, Any]:
    raw = str(status_item.get("status") or "").strip().lower()
    mapping = {
        "sent": "SENT",
        "delivered": "RECEIVED",
        "read": "READ",
        "failed": "FAILED",
    }
    return {
        "messageId": str(status_item.get("id") or ""),
        "status": mapping.get(raw, raw.upper() or "SENT"),
        "fromMe": True,
        "phone": str(status_item.get("recipient_id") or ""),
    }


class MetaWhatsAppService:
    """Mesma superfície usada pelos remetentes de WhatsApp (send_text etc.)."""

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        graph_api_version: str = DEFAULT_GRAPH_VERSION,
        app_secret: str = "",
    ) -> None:
        self.phone_number_id = (phone_number_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.graph_api_version = (graph_api_version or DEFAULT_GRAPH_VERSION).strip()
        self.app_secret = (app_secret or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.phone_number_id and self.access_token)

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise MetaWhatsAppNotConfigured(
                "A integração com a API oficial do Meta ainda não foi configurada."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _messages_url(self) -> str:
        self._ensure_configured()
        return (
            f"{GRAPH_BASE}/{self.graph_api_version}/{self.phone_number_id}/messages"
        )

    def _phone_url(self) -> str:
        self._ensure_configured()
        return (
            f"{GRAPH_BASE}/{self.graph_api_version}/{self.phone_number_id}"
            "?fields=display_phone_number,verified_name,quality_rating"
        )

    @staticmethod
    def normalize_phone(phone: str | None) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) in {10, 11}:
            digits = "55" + digits
        if not 12 <= len(digits) <= 15:
            raise MetaWhatsAppError("Informe um telefone válido com DDI e DDD.")
        return digits

    @staticmethod
    def _assert_recipient_not_blacklisted(phone: str) -> None:
        from app.services.global_blacklist import (
            BlacklistedRecipientError,
            assert_outbound_permitted,
        )

        try:
            assert_outbound_permitted(phone)
        except BlacklistedRecipientError as exc:
            raise ZApiBlacklistedRecipient(str(exc)) from exc

    @staticmethod
    def _safe_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("error_user_msg")
                if message:
                    return str(message)[:240]
            message = payload.get("message")
            if message:
                return str(message)[:240]
        return f"HTTP {response.status_code}"

    def _normalize_send_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages")
        message_id = ""
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = str(messages[0].get("id") or "").strip()
        return {
            "messageId": message_id,
            "id": message_id,
            "messages": messages if isinstance(messages, list) else [],
            "contacts": payload.get("contacts") if isinstance(payload.get("contacts"), list) else [],
            "provider": PROVIDER_META,
        }

    def _post_message(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self._messages_url(),
                    headers=self._headers(),
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise MetaWhatsAppError(
                "Não foi possível enviar a mensagem pela API oficial do Meta."
            ) from exc
        if response.status_code >= 400:
            raise MetaWhatsAppError(
                f"Falha no envio pela API oficial do Meta: {self._safe_error(response)}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaWhatsAppError(
                "A API oficial do Meta retornou uma resposta inesperada."
            ) from exc
        if not isinstance(payload, dict):
            raise MetaWhatsAppError(
                "A API oficial do Meta retornou uma resposta inesperada."
            )
        return self._normalize_send_result(payload)

    def status(self) -> dict[str, Any]:
        info = self.phone_info()
        return {
            "configured": True,
            "connected": True,
            "smartphone_connected": True,
            "error": None,
            "display_phone_number": info.get("display_phone_number"),
            "verified_name": info.get("verified_name"),
            "quality_rating": info.get("quality_rating"),
            "provider": PROVIDER_META,
        }

    def phone_info(self) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self._phone_url(), headers=self._headers())
        except httpx.HTTPError as exc:
            raise MetaWhatsAppError(
                "Não foi possível consultar o número na API oficial do Meta."
            ) from exc
        if response.status_code >= 400:
            raise MetaWhatsAppError(
                f"Falha ao consultar número Meta: {self._safe_error(response)}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaWhatsAppError(
                "A API oficial do Meta retornou dados inválidos do número."
            ) from exc
        if not isinstance(payload, dict):
            raise MetaWhatsAppError(
                "A API oficial do Meta retornou dados inválidos do número."
            )
        return {
            "display_phone_number": str(payload.get("display_phone_number") or "").strip(),
            "verified_name": str(payload.get("verified_name") or "").strip(),
            "quality_rating": str(payload.get("quality_rating") or "").strip(),
            "id": str(payload.get("id") or self.phone_number_id).strip(),
        }

    def device(self) -> dict[str, Any]:
        info = self.phone_info()
        raw_phone = re.sub(r"\D", "", info.get("display_phone_number") or "")
        return {
            "phone": raw_phone,
            "name": info.get("verified_name") or "",
            "img_url": "",
            "about": "",
            "is_business": True,
            "original_device": "meta_cloud_api",
            "session_name": "",
            "device_model": "Meta Cloud API",
        }

    def send_template(self, *, phone: str, template: dict[str, Any]) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        self._assert_recipient_not_blacklisted(normalized_phone)
        return self._post_message({
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": normalized_phone, "type": "template", "template": template,
        })

    def send_text(self, *, phone: str, message: str) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        self._assert_recipient_not_blacklisted(normalized_phone)
        clean_message = message.strip()
        if not clean_message:
            raise MetaWhatsAppError("A mensagem não pode ficar vazia.")
        if len(clean_message) > 4096:
            raise MetaWhatsAppError("A mensagem deve ter no máximo 4.096 caracteres.")
        return self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalized_phone,
                "type": "text",
                "text": {"preview_url": False, "body": clean_message},
            }
        )

    def send_button_list(
        self,
        *,
        phone: str,
        message: str,
        buttons: list[dict[str, str]],
    ) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        self._assert_recipient_not_blacklisted(normalized_phone)
        clean_message = message.strip()
        if not clean_message:
            raise MetaWhatsAppError("A mensagem não pode ficar vazia.")
        if len(clean_message) > 1024:
            raise MetaWhatsAppError(
                "Na API oficial do Meta, o texto com botões deve ter no máximo 1.024 caracteres."
            )

        clean_buttons: list[dict[str, Any]] = []
        for index, button in enumerate(buttons[:3], start=1):
            label = str(button.get("label") or "").strip()
            if not label:
                continue
            if len(label) > 20:
                raise MetaWhatsAppError("Cada botão deve ter no máximo 20 caracteres.")
            button_id = str(button.get("id") or f"reply_{index}").strip()[:256]
            clean_buttons.append(
                {
                    "type": "reply",
                    "reply": {"id": button_id, "title": label},
                }
            )
        if not clean_buttons:
            raise MetaWhatsAppError("Informe ao menos um botão de resposta.")

        return self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalized_phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": clean_message},
                    "action": {"buttons": clean_buttons},
                },
            }
        )

    def send_link_button(
        self,
        *,
        phone: str,
        message: str,
        label: str,
        url: str,
    ) -> dict[str, Any]:
        # Cloud API não tem botão URL genérico fora de templates; envia texto + URL.
        clean_label = (label or "Abrir link").strip()[:40]
        clean_url = (url or "").strip()
        combined = f"{message.strip()}\n\n{clean_label}: {clean_url}".strip()
        return self.send_text(phone=phone, message=combined)

    def _send_media_link(
        self,
        *,
        phone: str,
        media_type: str,
        link: str,
        caption: str = "",
        filename: str | None = None,
    ) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        self._assert_recipient_not_blacklisted(normalized_phone)
        clean_url = link.strip()
        if not clean_url:
            raise MetaWhatsAppError("A mídia não pode ficar vazia.")
        media_body: dict[str, Any] = {"link": clean_url}
        if caption.strip() and media_type in {"image", "video", "document"}:
            media_body["caption"] = caption.strip()[:1024]
        if filename and media_type == "document":
            media_body["filename"] = filename.strip()[:240]
        return self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalized_phone,
                "type": media_type,
                media_type: media_body,
            }
        )

    def send_image(self, *, phone: str, image_url: str, caption: str = "") -> dict[str, Any]:
        return self._send_media_link(
            phone=phone, media_type="image", link=image_url, caption=caption
        )

    def send_video(self, *, phone: str, video_url: str, caption: str = "") -> dict[str, Any]:
        return self._send_media_link(
            phone=phone, media_type="video", link=video_url, caption=caption
        )

    def send_audio(self, *, phone: str, audio_url: str) -> dict[str, Any]:
        return self._send_media_link(phone=phone, media_type="audio", link=audio_url)

    def send_document(
        self,
        *,
        phone: str,
        document_url: str,
        extension: str,
        file_name: str,
        caption: str = "",
    ) -> dict[str, Any]:
        clean_extension = re.sub(r"[^a-zA-Z0-9]", "", extension or "").lower()
        name = (file_name or "").strip() or (
            f"arquivo.{clean_extension}" if clean_extension else "arquivo"
        )
        return self._send_media_link(
            phone=phone,
            media_type="document",
            link=document_url,
            caption=caption,
            filename=name,
        )
