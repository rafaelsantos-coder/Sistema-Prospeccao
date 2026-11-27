"""Gateway proprio Sulnet: cliente de rede privada, controle de fila e HMAC.

O codigo OpenWA e dependencias dele NAO sao importados pelo Prospecta.
Todas as mensagens do motor so entram com session_id vinculado a uma fila exata.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    ConversationSulnetWAInstance, ServiceQueue, ServiceQueueMetaAccount,
    ServiceQueueSulnetWAInstance, ServiceQueueZApiInstance, SulnetWAInstance,
)
from app.services.zapi import ZApiError, ZApiNotConfigured, ZApiBlacklistedRecipient

PROVIDER_SULNET_WA = "SULNET_WA"
_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9-]{3,80}$")
_PHONE_PATTERN = re.compile(r"^[1-9][0-9]{9,14}$")


class SulnetWAError(ZApiError):
    """Não inclui tokens, URLs privadas ou conteúdo de terceiros."""


@dataclass(frozen=True)
class GatewayConfig:
    enabled: bool
    base_url: str
    allowed_host: str
    api_key: str
    webhook_key: str

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("SULNET_WA_ENABLED", "").lower() in {"true", "1", "yes", "on"},
            base_url=os.getenv("SULNET_WA_BASE_URL", "").strip(),
            allowed_host=os.getenv("SULNET_WA_ALLOWED_HOST", "").strip().lower(),
            api_key=os.getenv("SULNET_WA_API_KEY", "").strip(),
            webhook_key=os.getenv("SULNET_WA_WEBHOOK_KEY", "").strip(),
        )

    @property
    def configured(self):
        return (self.enabled and len(self.api_key) >= 40
                and len(self.webhook_key) >= 40 and bool(self.base_url and self.allowed_host))

    def validate_target(self):
        if not self.configured:
            raise SulnetWAError("Motor Sulnet WhatsApp ainda não foi habilitado.")
        try:
            u = urlsplit(self.base_url)
            host = (u.hostname or "").lower()
            port = u.port
        except ValueError as exc:
            raise SulnetWAError("Destino do motor inválido.") from exc
        if (host != self.allowed_host or not host.endswith(".railway.internal")
                or u.scheme != "http" or not port or port > 65535
                or u.path not in {"", "/"} or u.query or u.fragment
                or u.username or u.password):
            raise SulnetWAError("Somente um destino privado Railway autorizado pode ser usado.")
        return self.base_url.rstrip("/")


def valid_session_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SESSION_PATTERN.fullmatch(value))


class GatewayClient:
    def __init__(self, config=None, *, transport=None):
        self.config = config or GatewayConfig.from_env()
        self.transport = transport

    def request(self, method, path, *, payload=None):
        base = self.config.validate_target()
        if method not in {"GET", "POST"} or not re.fullmatch(
            r"/v1/sessions(?:/[A-Za-z0-9-]{3,80}(?:/(?:start|stop|qr|send))?)?", path
        ):
            raise SulnetWAError("Operação não autorizada.")
        try:
            with httpx.Client(timeout=httpx.Timeout(9.0, connect=3.0),
                              follow_redirects=False, trust_env=False, transport=self.transport) as client:
                with client.stream(method, base + path,
                                   headers={"Authorization": "Bearer " + self.config.api_key,
                                            "Accept": "application/json"},
                                   json=payload) as response:
                    if response.status_code not in (200, 201):
                        raise SulnetWAError("Gateway recusou a operação.")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > 48_000:
                            raise SulnetWAError("Resposta do motor acima do limite.")
                        raw.extend(chunk)
                    data = json.loads(raw)
        except (httpx.HTTPError, ValueError) as exc:
            raise SulnetWAError("Motor Sulnet WhatsApp indisponível.") from exc
        if not isinstance(data, (dict, list)):
            raise SulnetWAError("Resposta inválida do motor.")
        return data

    def list_sessions(self):
        data = self.request("GET", "/v1/sessions")
        if not isinstance(data, list):
            raise SulnetWAError("Resposta inválida do motor.")
        return [sanitize_session(x) for x in data[:100] if isinstance(x, dict)]

    def create(self, session_id):
        if not valid_session_id(session_id):
            raise SulnetWAError("ID de sessão inválido.")
        return sanitize_session(self.request("POST", "/v1/sessions", payload={"id": session_id}))

    def status(self, session_id):
        if not valid_session_id(session_id):
            raise SulnetWAError("ID de sessão inválido.")
        data = self.request("GET", "/v1/sessions/" + session_id)
        if not isinstance(data, dict) or data.get("id") != session_id:
            raise SulnetWAError("Sessão retornada não corresponde à solicitada.")
        return sanitize_session(data)

    def start(self, session_id):
        if not valid_session_id(session_id):
            raise SulnetWAError("ID de sessão inválido.")
        return sanitize_session(self.request("POST", "/v1/sessions/" + session_id + "/start"))

    def stop(self, session_id):
        if not valid_session_id(session_id):
            raise SulnetWAError("ID de sessão inválido.")
        return sanitize_session(self.request("POST", "/v1/sessions/" + session_id + "/stop"))

    def qr(self, session_id):
        if not valid_session_id(session_id):
            raise SulnetWAError("ID de sessão inválido.")
        data = self.request("GET", "/v1/sessions/" + session_id + "/qr")
        value = data.get("qrDataUrl") if isinstance(data, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"data:image/png;base64,[A-Za-z0-9+/=]{100,40000}", value):
            raise SulnetWAError("QR Code do motor indisponível ou inválido.")
        return value

    def send_text(self, session_id, *, phone, message):
        if not valid_session_id(session_id) or not _PHONE_PATTERN.fullmatch(phone):
            raise SulnetWAError("Destino ou sessão inválidos.")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4096:
            raise SulnetWAError("Mensagem inválida.")
        result = self.request("POST", "/v1/sessions/" + session_id + "/send",
                              payload={"phone": phone, "text": message.strip()})
        msg_id = result.get("messageId") if isinstance(result, dict) else None
        if not isinstance(msg_id, str) or not msg_id.startswith("swa:" + session_id + ":"):
            raise SulnetWAError("Envio sem confirmação verificável do provedor.")
        return {"messageId": msg_id, "id": msg_id, "provider": PROVIDER_SULNET_WA}


def sanitize_session(value):
    if not isinstance(value, dict):
        raise SulnetWAError("Estado inválido do gateway.")
    status = value.get("status")
    return {
        "id": str(value.get("id") or "")[:80],
        "status": status if isinstance(status, str) and status in {"created", "initializing", "qr_ready", "authenticating",
                                      "ready", "disconnected", "action_required", "failed"} else "unknown",
        "phone": str(value.get("phone") or "")[:20],
        "engineLoaded": value.get("engineLoaded") is True,
    }


def verify_event(body: bytes, timestamp: str, signature: str, config=None) -> dict:
    cfg = config or GatewayConfig.from_env()
    if not cfg.configured or len(body) > 8192 or not re.fullmatch(r"[0-9]{10,12}", timestamp):
        raise SulnetWAError("Evento não autorizado.")
    if abs(int(time.time()) - int(timestamp)) > 300:
        raise SulnetWAError("Evento fora do prazo permitido.")
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise SulnetWAError("Assinatura inválida.")
    expected = hmac.new(cfg.webhook_key.encode(), timestamp.encode() + b"." + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SulnetWAError("Assinatura inválida.")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SulnetWAError("Corpo do evento inválido.") from exc
    if not isinstance(data, dict):
        raise SulnetWAError("Evento inválido.")
    sid, msg, phone, eid = (data.get(k) for k in ("sessionId", "messageId", "from", "eventId"))
    if (not valid_session_id(sid) or not isinstance(msg, str) or not 3 <= len(msg) <= 180
            or not isinstance(phone, str) or not _PHONE_PATTERN.fullmatch(phone)
            or not isinstance(eid, str) or not re.fullmatch(r"[a-f0-9]{64}", eid)
            or eid != hashlib.sha256((sid + "\0" + msg).encode()).hexdigest()
            or data.get("source") != PROVIDER_SULNET_WA
            or not isinstance(data.get("text"), str) or len(data["text"]) > 4096):
        raise SulnetWAError("Evento malformado ou fora do escopo.")
    return data


def list_instances(db: Session):
    return list(db.scalars(select(SulnetWAInstance).order_by(SulnetWAInstance.id)))


def queue_instance(db: Session, queue_id: int | None):
    if queue_id is None:
        return None
    return db.scalar(
        select(SulnetWAInstance)
        .join(ServiceQueueSulnetWAInstance,
              ServiceQueueSulnetWAInstance.instance_id == SulnetWAInstance.id)
        .where(ServiceQueueSulnetWAInstance.queue_id == queue_id,
               SulnetWAInstance.active.is_(True))
    )


def queue_for_instance(db: Session, instance_id: int):
    return db.scalar(
        select(ServiceQueue)
        .join(ServiceQueueSulnetWAInstance,
              ServiceQueueSulnetWAInstance.queue_id == ServiceQueue.id)
        .where(ServiceQueueSulnetWAInstance.instance_id == instance_id,
               ServiceQueue.active.is_(True))
    )


def conversation_instance(db: Session, conversation_id: int):
    return db.scalar(
        select(SulnetWAInstance)
        .join(ConversationSulnetWAInstance,
              ConversationSulnetWAInstance.instance_id == SulnetWAInstance.id)
        .where(ConversationSulnetWAInstance.conversation_id == conversation_id)
    )


def bind_conversation_instance(db: Session, conversation_id: int, instance_id: int):
    item = db.scalar(select(ConversationSulnetWAInstance).where(
        ConversationSulnetWAInstance.conversation_id == conversation_id))
    if item and item.instance_id != instance_id:
        raise SulnetWAError("Mistura de instâncias bloqueada.")
    if not item:
        db.add(ConversationSulnetWAInstance(conversation_id=conversation_id, instance_id=instance_id))
        db.flush()


def bind_queue_instance(db: Session, queue_id: int, instance_id: int):
    instance = db.get(SulnetWAInstance, instance_id)
    existing_binding = db.scalar(select(ServiceQueueSulnetWAInstance).where(
        ServiceQueueSulnetWAInstance.instance_id == instance_id
    ))
    if (not instance or not instance.active
            or (existing_binding is not None and existing_binding.queue_id != queue_id)):
        raise SulnetWAError("Instância inválida ou já vinculada a outra fila.")
    db.execute(delete(ServiceQueueMetaAccount).where(ServiceQueueMetaAccount.queue_id == queue_id))
    db.execute(delete(ServiceQueueZApiInstance).where(ServiceQueueZApiInstance.queue_id == queue_id))
    previous = db.scalar(select(ServiceQueueSulnetWAInstance).where(
        ServiceQueueSulnetWAInstance.queue_id == queue_id))
    if previous:
        previous.instance_id = instance_id
    else:
        db.add(ServiceQueueSulnetWAInstance(queue_id=queue_id, instance_id=instance_id))
    db.flush()


def clear_queue_binding(db: Session, queue_id: int):
    db.execute(delete(ServiceQueueSulnetWAInstance).where(
        ServiceQueueSulnetWAInstance.queue_id == queue_id))


class SulnetWAService:
    """Superficie de envio reutilizada no Prospecta."""

    def __init__(self, instance: SulnetWAInstance):
        self.instance = instance

    @property
    def configured(self):
        return self.instance.active and GatewayConfig.from_env().configured

    def send_text(self, *, phone, message):
        from app.services.global_blacklist import (
            assert_outbound_permitted, BlacklistedRecipientError, BlacklistPhoneError,
        )
        try:
            assert_outbound_permitted(phone)
        except (BlacklistedRecipientError, BlacklistPhoneError) as exc:
            raise ZApiBlacklistedRecipient(str(exc)) from exc
        if not self.instance.active:
            raise ZApiNotConfigured("Instância Sulnet WhatsApp desativada.")
        return GatewayClient().send_text(self.instance.session_id, phone=phone, message=message)

    def send_image(self, **kwargs):
        raise SulnetWAError("Mídia ainda não homologada para o motor Sulnet.")

    send_video = send_image
    send_audio = send_image
    send_document = send_image
