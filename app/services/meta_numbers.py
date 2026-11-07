"""Números da API oficial da Meta: cadastro, vínculo com filas e conversas,
janela de 24h e templates.

Espelha ``zapi_instances`` sem tocar nele: as tabelas são outras e o vínculo
Z-API de uma fila continua salvo mesmo quando ela passa a enviar pela Meta.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Contact,
    Conversation,
    ConversationMetaNumber,
    Message,
    MetaMessageTemplate,
    MetaWhatsAppNumber,
    ServiceQueue,
    ServiceQueueMetaNumber,
)
from app.services.meta_cloud import MetaCloudError, MetaCloudService, template_param_text
from app.services.secret_store import decrypt_secret, encrypt_secret
from app.timeutils import utcnow


# A Meta conta 24h a partir da última mensagem do cliente. Margem de 10 min
# para não mandar texto livre no limite e a Meta recusar.
CUSTOMER_WINDOW = timedelta(hours=24) - timedelta(minutes=10)
WEBHOOK_PATH = "/webhooks/meta"


# ---------------------------------------------------------------- telefone


def whatsapp_id_to_phone(wa_id: str | None) -> str:
    """Telefone no formato dos leads a partir do wa_id da Meta.

    Celulares brasileiros antigos chegam da Meta sem o nono dígito
    (55 + DDD + 8). Os leads guardam com o 9; sem isso o mesmo cliente viraria
    outro contato.
    """
    digits = re.sub(r"\D", "", wa_id or "")
    if len(digits) == 12 and digits.startswith("55") and digits[4] in "6789":
        return digits[:4] + "9" + digits[4:]
    return digits


def phone_variants(phone: str | None) -> set[str]:
    digits = whatsapp_id_to_phone(phone)
    if not digits:
        return set()
    variants = {digits}
    if len(digits) == 13 and digits.startswith("55") and digits[4] == "9":
        variants.add(digits[:4] + digits[5:])
    return variants


# ------------------------------------------------------------------ cadastro


def list_meta_numbers(db: Session, *, active_only: bool = True) -> list[MetaWhatsAppNumber]:
    stmt = select(MetaWhatsAppNumber)
    if active_only:
        stmt = stmt.where(MetaWhatsAppNumber.active.is_(True))
    return list(db.scalars(stmt.order_by(MetaWhatsAppNumber.name, MetaWhatsAppNumber.id)))


def get_meta_number(db: Session, number_id: int | None) -> MetaWhatsAppNumber | None:
    if not number_id:
        return None
    return db.get(MetaWhatsAppNumber, number_id)


def meta_number_by_phone_number_id(db: Session, phone_number_id: str) -> MetaWhatsAppNumber | None:
    clean = str(phone_number_id or "").strip()
    if not clean:
        return None
    return db.scalar(
        select(MetaWhatsAppNumber).where(
            MetaWhatsAppNumber.phone_number_id == clean,
            MetaWhatsAppNumber.active.is_(True),
        )
    )


def create_meta_number(
    db: Session,
    *,
    name: str,
    phone_number_id: str,
    waba_id: str,
    access_token: str,
    app_secret: str,
    created_by_id: int | None,
    graph_version: str = "",
) -> MetaWhatsAppNumber:
    item = MetaWhatsAppNumber(
        name=name.strip(),
        phone_number_id=phone_number_id.strip(),
        waba_id=waba_id.strip(),
        graph_version=graph_version.strip(),
        access_token_encrypted=encrypt_secret(access_token.strip()),
        app_secret_encrypted=encrypt_secret(app_secret.strip()),
        verify_token_encrypted=encrypt_secret(secrets.token_urlsafe(32)),
        active=True,
        created_by_id=created_by_id,
    )
    db.add(item)
    db.flush()
    return item


def service_for_meta_number(number: MetaWhatsAppNumber) -> MetaCloudService:
    return MetaCloudService(
        phone_number_id=number.phone_number_id,
        access_token=decrypt_secret(number.access_token_encrypted),
        waba_id=number.waba_id,
        verify_token=decrypt_secret(number.verify_token_encrypted),
        graph_version=number.graph_version or None,
    )


def meta_app_secret(number: MetaWhatsAppNumber) -> str:
    return decrypt_secret(number.app_secret_encrypted)


def meta_verify_token(number: MetaWhatsAppNumber) -> str:
    return decrypt_secret(number.verify_token_encrypted)


def meta_webhook_url() -> str:
    if not settings.app_public_url:
        return ""
    return f"{settings.app_public_url}{WEBHOOK_PATH}"


def refresh_meta_number_info(number: MetaWhatsAppNumber) -> dict[str, Any]:
    info = service_for_meta_number(number).phone_info()
    if info.get("display_phone_number"):
        number.phone_label = info["display_phone_number"]
    if info.get("verified_name"):
        number.verified_name = info["verified_name"]
    number.quality_rating = info.get("quality_rating") or number.quality_rating
    return info


# ----------------------------------------------------------------- vínculos


def queue_meta_number(db: Session, queue_id: int | None) -> MetaWhatsAppNumber | None:
    if not queue_id:
        return None
    return db.scalar(
        select(MetaWhatsAppNumber)
        .join(ServiceQueueMetaNumber, ServiceQueueMetaNumber.meta_number_id == MetaWhatsAppNumber.id)
        .where(
            ServiceQueueMetaNumber.queue_id == queue_id,
            MetaWhatsAppNumber.active.is_(True),
        )
    )


def bind_queue_meta_number(db: Session, queue_id: int, meta_number_id: int) -> ServiceQueueMetaNumber:
    link = db.scalar(select(ServiceQueueMetaNumber).where(ServiceQueueMetaNumber.queue_id == queue_id))
    if not link:
        link = ServiceQueueMetaNumber(queue_id=queue_id, meta_number_id=meta_number_id)
        db.add(link)
    else:
        link.meta_number_id = meta_number_id
    db.flush()
    return link


def unbind_queue_meta_number(db: Session, queue_id: int) -> bool:
    """A fila volta a enviar pela Z-API (o vínculo Z-API nunca foi apagado)."""
    link = db.scalar(select(ServiceQueueMetaNumber).where(ServiceQueueMetaNumber.queue_id == queue_id))
    if not link:
        return False
    db.delete(link)
    db.flush()
    return True


def conversation_meta_number(db: Session, conversation_id: int | None) -> MetaWhatsAppNumber | None:
    if not conversation_id:
        return None
    return db.scalar(
        select(MetaWhatsAppNumber)
        .join(ConversationMetaNumber, ConversationMetaNumber.meta_number_id == MetaWhatsAppNumber.id)
        .where(
            ConversationMetaNumber.conversation_id == conversation_id,
            MetaWhatsAppNumber.active.is_(True),
        )
    )


def bind_conversation_meta_number(
    db: Session,
    conversation_id: int,
    meta_number_id: int,
) -> ConversationMetaNumber:
    link = db.scalar(
        select(ConversationMetaNumber).where(ConversationMetaNumber.conversation_id == conversation_id)
    )
    if not link:
        link = ConversationMetaNumber(conversation_id=conversation_id, meta_number_id=meta_number_id)
        db.add(link)
    else:
        link.meta_number_id = meta_number_id
    db.flush()
    return link


def default_queue_for_meta_number(db: Session, meta_number_id: int) -> ServiceQueue | None:
    stmt = (
        select(ServiceQueue)
        .join(ServiceQueueMetaNumber, ServiceQueueMetaNumber.queue_id == ServiceQueue.id)
        .where(
            ServiceQueueMetaNumber.meta_number_id == meta_number_id,
            ServiceQueue.active.is_(True),
        )
        .order_by(ServiceQueue.is_default_inbound.desc(), ServiceQueue.id.asc())
    )
    return db.scalar(stmt.limit(1))


# ------------------------------------------------------------ janela de 24h


def meta_window_open(db: Session, meta_number_id: int, phone: str | None) -> bool:
    """O cliente mandou mensagem para este número Meta nas últimas 24h?"""
    variants = phone_variants(phone)
    if not variants:
        return False
    since = utcnow() - CUSTOMER_WINDOW
    last_inbound = db.scalar(
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Contact, Contact.id == Conversation.contact_id)
        .join(ConversationMetaNumber, ConversationMetaNumber.conversation_id == Conversation.id)
        .where(
            Contact.phone.in_(variants),
            ConversationMetaNumber.meta_number_id == meta_number_id,
            Message.direction == "INBOUND",
            Message.created_at >= since,
        )
        .limit(1)
    )
    return last_inbound is not None


# ---------------------------------------------------------------- templates


_VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def template_variables(text: str | None) -> list[str]:
    seen: list[str] = []
    for name in _VARIABLE_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    if seen and all(item.isdigit() for item in seen):
        seen.sort(key=int)
    return seen


def _components(template: MetaMessageTemplate) -> list[dict[str, Any]]:
    try:
        value = json.loads(template.components_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def template_unsupported_reason(template: MetaMessageTemplate) -> str:
    """Motivo para o template não poder ser usado no disparo automático ("" se pode)."""
    for component in _components(template):
        kind = str(component.get("type") or "").upper()
        if kind == "HEADER" and str(component.get("format") or "").upper() == "TEXT":
            if template_variables(str(component.get("text") or "")):
                return "cabeçalho de texto com variável"
        if kind == "HEADER" and str(component.get("format") or "").upper() == "LOCATION":
            return "cabeçalho de localização"
        if kind == "BUTTONS":
            for button in component.get("buttons") or []:
                if not isinstance(button, dict):
                    continue
                btype = str(button.get("type") or "").upper()
                if btype == "URL" and template_variables(str(button.get("url") or "")):
                    return "botão de link com variável"
                if btype in {"COPY_CODE", "OTP", "FLOW", "CATALOG", "MPM"}:
                    return f"botão do tipo {btype}"
        if kind in {"CAROUSEL", "LIMITED_TIME_OFFER"}:
            return f"componente {kind}"
    return ""


def template_quick_replies(template: MetaMessageTemplate) -> list[str]:
    labels: list[str] = []
    for component in _components(template):
        if str(component.get("type") or "").upper() != "BUTTONS":
            continue
        for button in component.get("buttons") or []:
            if isinstance(button, dict) and str(button.get("type") or "").upper() == "QUICK_REPLY":
                labels.append(str(button.get("text") or ""))
    return [item for item in labels if item]


def build_template_message(
    template: MetaMessageTemplate,
    values: list[str],
    *,
    header_media_url: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Componentes da Cloud API + texto final (para o histórico da conversa)."""
    reason = template_unsupported_reason(template)
    if reason:
        raise MetaCloudError(f"O template {template.name} usa {reason}, que o disparo não suporta.")
    variables = template_variables(template.body_text)
    if len(values) != len(variables):
        raise MetaCloudError(
            f"O template {template.name} tem {len(variables)} variável(is), "
            f"mas a campanha preencheu {len(values)}."
        )
    clean_values = [template_param_text(value) for value in values]
    if any(not value for value in clean_values):
        raise MetaCloudError(
            "Uma variável do template ficou vazia para este lead. "
            "Use um valor padrão na campanha, ex.: {{nome}}|cliente."
        )

    components: list[dict[str, Any]] = []
    header_format = (template.header_format or "").upper()
    if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
        if not header_media_url:
            raise MetaCloudError(
                f"O template {template.name} exige {header_format.lower()} no cabeçalho. "
                "Anexe a mídia na campanha."
            )
        kind = header_format.lower()
        components.append(
            {"type": "header", "parameters": [{"type": kind, kind: {"link": header_media_url}}]}
        )

    if variables:
        named = (template.parameter_format or "").upper() == "NAMED" or not all(
            item.isdigit() for item in variables
        )
        parameters: list[dict[str, Any]] = []
        for name, value in zip(variables, clean_values, strict=True):
            parameter: dict[str, Any] = {"type": "text", "text": value}
            if named:
                parameter["parameter_name"] = name
            parameters.append(parameter)
        components.append({"type": "body", "parameters": parameters})

    rendered = template.body_text or template.name
    for name, value in zip(variables, clean_values, strict=True):
        rendered = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", lambda _m, v=value: v, rendered)
    return components, rendered.strip()


def _template_from_api(item: dict[str, Any]) -> dict[str, Any]:
    components = [c for c in (item.get("components") or []) if isinstance(c, dict)]
    body_text = ""
    header_format = ""
    for component in components:
        kind = str(component.get("type") or "").upper()
        if kind == "BODY":
            body_text = str(component.get("text") or "")
        if kind == "HEADER":
            header_format = str(component.get("format") or "").upper()
    return {
        "meta_template_id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "language": str(item.get("language") or ""),
        "category": str(item.get("category") or "").upper(),
        "status": str(item.get("status") or "").upper(),
        "parameter_format": str(item.get("parameter_format") or "POSITIONAL").upper(),
        "header_format": header_format,
        "body_text": body_text,
        "components_json": json.dumps(components, ensure_ascii=False),
    }


def sync_meta_templates(db: Session, number: MetaWhatsAppNumber) -> int:
    """Traz os templates da WABA. Templates que sumiram da Meta ficam marcados DELETED."""
    items = service_for_meta_number(number).list_templates()
    now = utcnow()
    existing = {
        (item.name, item.language): item
        for item in db.scalars(
            select(MetaMessageTemplate).where(MetaMessageTemplate.meta_number_id == number.id)
        )
    }
    seen: set[tuple[str, str]] = set()
    for raw in items:
        data = _template_from_api(raw)
        if not data["name"] or not data["language"]:
            continue
        key = (data["name"], data["language"])
        seen.add(key)
        record = existing.get(key)
        if not record:
            record = MetaMessageTemplate(meta_number_id=number.id, **data, synced_at=now)
            db.add(record)
        else:
            for field, value in data.items():
                setattr(record, field, value)
            record.synced_at = now
    for key, record in existing.items():
        if key not in seen:
            record.status = "DELETED"
            record.synced_at = now
    number.templates_synced_at = now
    db.flush()
    return len(seen)


def approved_templates(db: Session) -> list[MetaMessageTemplate]:
    return list(
        db.scalars(
            select(MetaMessageTemplate)
            .join(MetaWhatsAppNumber, MetaWhatsAppNumber.id == MetaMessageTemplate.meta_number_id)
            .where(
                MetaMessageTemplate.status == "APPROVED",
                MetaWhatsAppNumber.active.is_(True),
            )
            .order_by(MetaMessageTemplate.name, MetaMessageTemplate.language)
        )
    )
