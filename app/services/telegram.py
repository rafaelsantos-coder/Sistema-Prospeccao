"""Canal Telegram (bot oficial) com a mesma interface de envio da Z-API.

Regras do canal:
- O bot só fala com quem já abriu a conversa pelo link ``t.me/<bot>?start=<token>``.
  Não existe envio a frio: telefone não é destino no Telegram.
- Cada lead da base tem no máximo um vínculo (``TelegramLink``). O token do link é
  aleatório e identifica o lead sem expor telefone.
- ``/parar`` (ou pedido explícito de parar) encerra o canal para aquele cliente.
- O envio reaproveita ``service_for_conversation``: a IA, o follow-up e o inbox
  falam pelo Telegram sem saber o canal, porque a fonte da conversa é ``TELEGRAM``.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Activity,
    AIQueueBinding,
    Conversation,
    Lead,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    TelegramLink,
)
from app.services.zapi import ZApiError, ZApiNotConfigured
from app.timeutils import utcnow

LOG = logging.getLogger("sulnet_telegram")

TELEGRAM_SOURCE = "TELEGRAM"
TELEGRAM_QUEUE_CODE = "telegram"
API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096
STOP_COMMANDS = {"/parar", "/stop", "/sair"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

WELCOME_NO_TOKEN = (
    "Olá! Eu sou o atendimento da Sulnet. Para falar com você por aqui, "
    "abra o link que enviamos no seu atendimento."
)
INVALID_LINK = "Este link não é mais válido. Peça um novo link ao seu consultor da Sulnet."
OTHER_ACCOUNT = "Este Telegram já está ligado a outro cadastro. Fale com a Sulnet para ajustar."
STOPPED_CONFIRMATION = (
    "Tudo certo, não vamos mais enviar mensagens por aqui. "
    "Se quiser voltar, é só pedir um novo link à Sulnet."
)
UNSUPPORTED_CONTENT = "Por aqui eu consigo ler só mensagens de texto. Pode escrever o que precisa?"


class TelegramError(ZApiError):
    """Falha no envio pelo Telegram. ``blocked`` indica que o cliente bloqueou o bot."""

    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


class TelegramNotConfigured(ZApiNotConfigured):
    pass


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_bot_username)


def _api_url(method: str) -> str:
    if not settings.telegram_bot_token:
        raise TelegramNotConfigured("O bot do Telegram ainda não foi configurado.")
    return f"{API_BASE}/bot{settings.telegram_bot_token}/{method}"


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(_api_url(method), json=payload)
    except httpx.HTTPError as exc:
        raise TelegramError("Não foi possível falar com o Telegram.") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
        description = str(data.get("description") or f"HTTP {response.status_code}")[:240]
        blocked = response.status_code == 403 or "blocked" in description.lower()
        raise TelegramError(f"Falha no envio pelo Telegram: {description}", blocked=blocked)
    result = data.get("result")
    return result if isinstance(result, dict) else {}


class TelegramService:
    """Mesma interface usada pelos remetentes de WhatsApp (send_text etc.)."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = str(chat_id)

    @property
    def configured(self) -> bool:
        return bool(settings.telegram_bot_token)

    def _send(self, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = (text or "").strip()
        if not clean:
            raise TelegramError("A mensagem não pode ficar vazia.")
        if len(clean) > MAX_TEXT:
            raise TelegramError("A mensagem deve ter no máximo 4.096 caracteres.")
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": clean}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = _call("sendMessage", payload)
        message_id = result.get("message_id")
        return {"messageId": f"tg:{self.chat_id}:{message_id}" if message_id else ""}

    def send_text(self, *, phone: str = "", message: str) -> dict[str, Any]:
        return self._send(message)

    def send_button_list(
        self, *, phone: str = "", message: str, buttons: list[dict[str, str]]
    ) -> dict[str, Any]:
        rows = []
        for button in buttons[:3]:
            label = str(button.get("label") or "").strip()[:40]
            if label:
                rows.append([{"text": label, "callback_data": label}])
        return self._send(message, {"inline_keyboard": rows} if rows else None)

    def send_link_button(
        self, *, phone: str = "", message: str, label: str, url: str
    ) -> dict[str, Any]:
        markup = {"inline_keyboard": [[{"text": label[:40], "url": url}]]}
        return self._send(message, markup)

    def _unsupported(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise TelegramError("Envio de mídia ainda não é suportado no Telegram.")

    send_image = send_video = send_audio = send_document = _unsupported


# ---------------------------------------------------------------------------
# Vínculo lead <-> chat
# ---------------------------------------------------------------------------

def get_or_create_link(db: Session, lead: Lead) -> TelegramLink | None:
    """Link do lead. Quem pediu para parar não recebe novo convite."""
    link = db.scalar(
        select(TelegramLink).where(TelegramLink.lead_id == lead.id).order_by(TelegramLink.id.desc()).limit(1)
    )
    if link and link.status == "PARADO":
        return None
    if link:
        return link
    link = TelegramLink(lead_id=lead.id, token=secrets.token_urlsafe(12), status="PENDENTE")
    db.add(link)
    db.flush()
    return link


def invite_url(link: TelegramLink) -> str:
    if not settings.telegram_bot_username:
        return ""
    return f"https://t.me/{settings.telegram_bot_username}?start={link.token}"


def invite_url_for_lead(db: Session, lead: Lead) -> str:
    if not configured() or lead.do_not_contact:
        return ""
    link = get_or_create_link(db, lead)
    return invite_url(link) if link else ""


def active_link_for_lead(db: Session, lead_id: int | None) -> TelegramLink | None:
    if not lead_id:
        return None
    return db.scalar(
        select(TelegramLink)
        .where(
            TelegramLink.lead_id == lead_id,
            TelegramLink.status == "ATIVO",
            TelegramLink.chat_id.is_not(None),
        )
        .order_by(TelegramLink.id.desc())
        .limit(1)
    )


def service_for_telegram_conversation(db: Session, conversation: Conversation) -> TelegramService:
    lead_id = conversation.lead_id or (conversation.contact.lead_id if conversation.contact else None)
    link = active_link_for_lead(db, lead_id)
    if not link or not link.chat_id:
        raise TelegramError("Este cliente não está com o Telegram ativo.")
    return TelegramService(link.chat_id)


# ---------------------------------------------------------------------------
# Fila do Telegram (IA e humanos, igual à fila padrão de entrada)
# ---------------------------------------------------------------------------

def ensure_telegram_queue(db: Session) -> ServiceQueue:
    queue = db.scalar(select(ServiceQueue).where(ServiceQueue.code == TELEGRAM_QUEUE_CODE))
    if queue:
        return queue
    base = db.scalar(
        select(ServiceQueue)
        .where(ServiceQueue.active.is_(True))
        .order_by(ServiceQueue.is_default_inbound.desc(), ServiceQueue.id)
        .limit(1)
    )
    queue = ServiceQueue(
        name="Telegram",
        code=TELEGRAM_QUEUE_CODE,
        team=base.team if base else "B2B",
        branch_id=base.branch_id if base else None,
        routing_strategy=base.routing_strategy if base else "ROUND_ROBIN",
        is_default_inbound=False,
        active=True,
    )
    db.add(queue)
    db.flush()
    if base:
        for member in db.scalars(
            select(ServiceQueueMember).where(
                ServiceQueueMember.queue_id == base.id, ServiceQueueMember.active.is_(True)
            )
        ):
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=member.user_id, priority=member.priority))
        binding = db.scalar(
            select(AIQueueBinding).where(AIQueueBinding.queue_id == base.id, AIQueueBinding.active.is_(True))
        )
        if binding:
            db.add(
                AIQueueBinding(
                    queue_id=queue.id,
                    agent_id=binding.agent_id,
                    handling_mode=binding.handling_mode,
                    active=True,
                    fallback_to_human=binding.fallback_to_human,
                    handoff_score_override=binding.handoff_score_override,
                )
            )
    db.flush()
    return queue


# ---------------------------------------------------------------------------
# Entrada (webhook)
# ---------------------------------------------------------------------------

def _reply(chat_id: str, text: str) -> None:
    try:
        TelegramService(chat_id).send_text(message=text)
    except ZApiError as exc:
        LOG.warning("telegram_reply_failed chat=%s error=%s", chat_id, str(exc)[:120])


def _answer_callback(callback_id: str) -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id})
    except ZApiError as exc:
        LOG.debug("telegram_answer_callback_failed error=%s", str(exc)[:120])


def _open_conversation(db: Session, lead: Lead) -> Conversation:
    from app.services.conversation_engine import ensure_contact, ensure_open_conversation

    queue = ensure_telegram_queue(db)
    contact = ensure_contact(db, lead.phone, lead=lead)
    return ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source=TELEGRAM_SOURCE,
        queue_id=queue.id,
        queue_origin="TELEGRAM",
    )


def _stop_link(db: Session, link: TelegramLink) -> None:
    link.status = "PARADO"
    link.stopped_at = utcnow()
    for conversation in db.scalars(
        select(Conversation).where(
            Conversation.lead_id == link.lead_id,
            Conversation.source == TELEGRAM_SOURCE,
            Conversation.closed_at.is_(None),
        )
    ):
        conversation.status = "BLOQUEADO"
        conversation.closed_at = utcnow()


def _record_inbound(
    db: Session, *, link: TelegramLink, chat_id: str, message_id: Any, text: str
) -> tuple[Conversation | None, Message | None]:
    from app.services.conversation_engine import register_message

    provider_id = f"tg:{chat_id}:{message_id}"
    if db.scalar(select(Message.id).where(Message.provider_message_id == provider_id)):
        return None, None  # reentrega do Telegram
    lead = db.get(Lead, link.lead_id)
    if not lead or not lead.phone:
        return None, None
    conversation = _open_conversation(db, lead)
    message = register_message(
        db,
        conversation,
        direction="INBOUND",
        message_type="TEXT",
        body=text,
        provider_message_id=provider_id,
        status="RECEIVED",
        increment_unread=True,
    )
    db.flush()
    now = utcnow()
    lead.last_contact_at = now
    lead.next_action_at = None
    if lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
        lead.stage = "CONTATO_REALIZADO"
    db.add(
        Activity(
            lead_id=lead.id,
            user_id=None,
            kind="TELEGRAM",
            status="CONCLUIDA",
            channel="TELEGRAM",
            outcome=f"RECEBIDA_TELEGRAM:{provider_id}"[:120],
            description="Mensagem recebida pelo Telegram; conteúdo disponível na conversa.",
            completed_at=now,
        )
    )
    return conversation, message


def process_update(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    from app.services.ai_sdr import enqueue_ai_job

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _answer_callback(str(callback.get("id") or ""))
        raw = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        message = {
            "chat": raw.get("chat") or {},
            "text": str(callback.get("data") or ""),
            "message_id": f"cb{callback.get('id')}",
            "from": callback.get("from") or {},
        }
    else:
        message = update.get("message")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "no_message"}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    if chat.get("type") != "private" or chat.get("id") is None:
        return {"ok": True, "ignored": "not_private"}
    chat_id = str(chat["id"])
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    username = str(sender.get("username") or "")[:80] or None
    text = str(message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""

    link_by_chat = db.scalar(select(TelegramLink).where(TelegramLink.chat_id == chat_id))

    if command == "/start":
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            if link_by_chat and link_by_chat.status == "ATIVO":
                text = "Olá"
            else:
                _reply(chat_id, WELCOME_NO_TOKEN)
                return {"ok": True, "ignored": "start_without_token"}
        else:
            link = db.scalar(select(TelegramLink).where(TelegramLink.token == token)) if _TOKEN_RE.match(token) else None
            if not link or (link.chat_id and link.chat_id != chat_id):
                _reply(chat_id, INVALID_LINK)
                return {"ok": True, "ignored": "invalid_token"}
            if link_by_chat and link_by_chat.id != link.id and link_by_chat.status == "ATIVO":
                _reply(chat_id, OTHER_ACCOUNT)
                return {"ok": True, "ignored": "chat_bound_elsewhere"}
            lead = db.get(Lead, link.lead_id)
            if not lead or lead.do_not_contact or link.status == "PARADO":
                _reply(chat_id, INVALID_LINK)
                return {"ok": True, "ignored": "blocked"}
            if link.status != "ATIVO":
                link.chat_id = chat_id
                link.username = username
                link.status = "ATIVO"
                link.started_at = utcnow()
            link_by_chat = link
            text = "Olá"
    elif command in STOP_COMMANDS:
        if link_by_chat and link_by_chat.status == "ATIVO":
            _stop_link(db, link_by_chat)
            db.commit()
        _reply(chat_id, STOPPED_CONFIRMATION)
        return {"ok": True, "stopped": True}

    if not link_by_chat or link_by_chat.status != "ATIVO":
        _reply(chat_id, WELCOME_NO_TOKEN)
        return {"ok": True, "ignored": "unlinked_chat"}
    if not text:
        _reply(chat_id, UNSUPPORTED_CONTENT)
        return {"ok": True, "ignored": "unsupported_content"}

    from app.routers.zapi import _is_explicit_opt_out

    conversation, stored = _record_inbound(
        db, link=link_by_chat, chat_id=chat_id, message_id=message.get("message_id"), text=text
    )
    if not conversation or not stored:
        db.commit()
        return {"ok": True, "ignored": "duplicate_or_unmatched"}
    opt_out = _is_explicit_opt_out(text)
    if opt_out:
        lead = db.get(Lead, link_by_chat.lead_id)
        if lead:
            lead.do_not_contact = True
            lead.stage = "NAO_CONTATAR"
        _stop_link(db, link_by_chat)
    else:
        enqueue_ai_job(db, conversation=conversation, message=stored)
    db.commit()
    return {"ok": True, "conversation_id": conversation.id, "opt_out": opt_out}


def set_webhook() -> dict[str, Any]:
    url = settings.telegram_webhook_url
    if not url:
        raise TelegramNotConfigured("Defina APP_PUBLIC_URL e TELEGRAM_WEBHOOK_SECRET.")
    payload: dict[str, Any] = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    # O Telegram devolve este segredo no cabeçalho de cada chamada.
    payload["secret_token"] = settings.telegram_webhook_secret
    return _call("setWebhook", payload) or {"ok": True}
