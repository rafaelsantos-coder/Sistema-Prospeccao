from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConversationZApiInstance,
    ServiceQueue,
    ServiceQueueZApiInstance,
    ZApiInstance,
)
from app.services.secret_store import (
    decrypt_secret,
    decrypt_secret_with_source,
    encrypt_secret,
)
from app.services.zapi import ZApiNotConfigured, ZApiService


def list_zapi_instances(db: Session, *, active_only: bool = True) -> list[ZApiInstance]:
    stmt = select(ZApiInstance)
    if active_only:
        stmt = stmt.where(ZApiInstance.active.is_(True))
    return list(db.scalars(stmt.order_by(ZApiInstance.name, ZApiInstance.id)))


def get_zapi_instance(db: Session, instance_id: int | None) -> ZApiInstance | None:
    if not instance_id:
        return None
    return db.get(ZApiInstance, instance_id)


MIN_WEBHOOK_KEY_LENGTH = 32


def instance_by_webhook_key(db: Session, webhook_key: str) -> ZApiInstance | None:
    if not webhook_key or len(webhook_key) < MIN_WEBHOOK_KEY_LENGTH:
        return None
    # Comparação em tempo constante: não revela a chave por diferença de tempo de resposta.
    for instance in db.scalars(select(ZApiInstance).where(ZApiInstance.active.is_(True))):
        if secrets.compare_digest(instance.webhook_key or "", webhook_key):
            return instance
    return None


def service_for_instance(instance: ZApiInstance) -> ZApiService:
    return ZApiService(
        base_url=instance.base_url,
        instance_id=instance.instance_id,
        instance_token=decrypt_secret(instance.instance_token_encrypted),
        client_token=decrypt_secret(instance.client_token_encrypted),
    )


def rotate_zapi_secrets(db: Session) -> int:
    """Recriptografa credenciais antigas com DATA_ENCRYPTION_KEY.

    A leitura continua aceitando a chave histórica durante a migração, então a
    rotação não interrompe instâncias existentes.
    """
    rotated = 0
    for instance in list(db.scalars(select(ZApiInstance))):
        changed = False
        if instance.instance_token_encrypted:
            plain, source = decrypt_secret_with_source(
                instance.instance_token_encrypted
            )
            if source != "primary":
                instance.instance_token_encrypted = encrypt_secret(plain)
                changed = True
        if instance.client_token_encrypted:
            plain, source = decrypt_secret_with_source(
                instance.client_token_encrypted
            )
            if source != "primary":
                instance.client_token_encrypted = encrypt_secret(plain)
                changed = True
        if changed:
            rotated += 1
    if rotated:
        db.flush()
    return rotated


def format_device_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("55") and len(digits) in {12, 13}:
        ddd = digits[2:4]
        local = digits[4:]
        if len(local) == 9:
            return f"+55 ({ddd}) {local[:5]}-{local[5:]}"
        if len(local) == 8:
            return f"+55 ({ddd}) {local[:4]}-{local[4:]}"
    return f"+{digits}" if digits else ""


def refresh_instance_device_info(instance: ZApiInstance) -> dict:
    info = service_for_instance(instance).device()
    raw_phone = str(info.get("phone") or "").strip()
    formatted_phone = format_device_phone(raw_phone)
    if formatted_phone:
        instance.phone_label = formatted_phone
    info["phone_raw"] = raw_phone
    info["phone_label"] = formatted_phone
    return info


def queue_instance(db: Session, queue_id: int | None) -> ZApiInstance | None:
    if not queue_id:
        return None
    return db.scalar(
        select(ZApiInstance)
        .join(
            ServiceQueueZApiInstance,
            ServiceQueueZApiInstance.zapi_instance_id == ZApiInstance.id,
        )
        .where(
            ServiceQueueZApiInstance.queue_id == queue_id,
            ZApiInstance.active.is_(True),
        )
    )


def bind_queue_instance(
    db: Session,
    queue_id: int,
    zapi_instance_id: int,
) -> ServiceQueueZApiInstance:
    link = db.scalar(
        select(ServiceQueueZApiInstance).where(
            ServiceQueueZApiInstance.queue_id == queue_id
        )
    )
    if not link:
        link = ServiceQueueZApiInstance(
            queue_id=queue_id,
            zapi_instance_id=zapi_instance_id,
        )
        db.add(link)
    else:
        link.zapi_instance_id = zapi_instance_id
    db.flush()
    return link


def conversation_instance(
    db: Session,
    conversation_id: int | None,
) -> ZApiInstance | None:
    if not conversation_id:
        return None
    return db.scalar(
        select(ZApiInstance)
        .join(
            ConversationZApiInstance,
            ConversationZApiInstance.zapi_instance_id == ZApiInstance.id,
        )
        .where(
            ConversationZApiInstance.conversation_id == conversation_id,
            ZApiInstance.active.is_(True),
        )
    )


def bind_conversation_instance(
    db: Session,
    conversation_id: int,
    zapi_instance_id: int,
) -> ConversationZApiInstance:
    link = db.scalar(
        select(ConversationZApiInstance).where(
            ConversationZApiInstance.conversation_id == conversation_id
        )
    )
    if not link:
        link = ConversationZApiInstance(
            conversation_id=conversation_id,
            zapi_instance_id=zapi_instance_id,
        )
        db.add(link)
    else:
        link.zapi_instance_id = zapi_instance_id
    db.flush()
    return link


def service_for_queue(db: Session, queue_id: int | None) -> ZApiService:
    """Serviço de envio da fila.

    Fila com número da API oficial da Meta vinculado envia pela Meta
    (``MetaCloudService`` tem os mesmos métodos de envio). Sem vínculo Meta,
    tudo segue pela Z-API como antes.
    """
    if queue_id:
        from app.services.meta_numbers import queue_meta_number, service_for_meta_number

        meta_number = queue_meta_number(db, queue_id)
        if meta_number:
            return service_for_meta_number(meta_number)  # type: ignore[return-value]
        instance = queue_instance(db, queue_id)
        if not instance:
            raise ZApiNotConfigured(
                "A fila selecionada não possui uma instância WhatsApp ativa configurada."
            )
        return service_for_instance(instance)
    return ZApiService()


def service_for_conversation(db: Session, conversation_id: int) -> ZApiService:
    """
    A fila é a fonte oficial da instância de saída.

    O vínculo salvo na conversa serve para histórico/entrada, mas nunca pode
    sobrescrever o número configurado atualmente na fila. Se estiver antigo,
    ele é autocorrigido antes do envio.
    """
    from app.services.meta_numbers import (
        bind_conversation_meta_number,
        conversation_meta_number,
        queue_meta_number,
        service_for_meta_number,
    )
    from app.services.queue_routing import queue_for_conversation

    queue = queue_for_conversation(db, conversation_id)
    if queue:
        meta_number = queue_meta_number(db, queue.id)
        if meta_number:
            current_meta = conversation_meta_number(db, conversation_id)
            if not current_meta or current_meta.id != meta_number.id:
                bind_conversation_meta_number(db, conversation_id, meta_number.id)
            return service_for_meta_number(meta_number)  # type: ignore[return-value]
        instance = queue_instance(db, queue.id)
        if not instance:
            raise ZApiNotConfigured(
                f"A fila {queue.name} não possui uma instância WhatsApp ativa configurada."
            )
        current = conversation_instance(db, conversation_id)
        if not current or current.id != instance.id:
            bind_conversation_instance(db, conversation_id, instance.id)
        return service_for_instance(instance)

    meta_number = conversation_meta_number(db, conversation_id)
    if meta_number:
        return service_for_meta_number(meta_number)  # type: ignore[return-value]
    instance = conversation_instance(db, conversation_id)
    if instance:
        return service_for_instance(instance)
    return ZApiService()


def default_queue_for_instance(
    db: Session,
    zapi_instance_id: int,
) -> ServiceQueue | None:
    stmt = (
        select(ServiceQueue)
        .join(
            ServiceQueueZApiInstance,
            ServiceQueueZApiInstance.queue_id == ServiceQueue.id,
        )
        .where(
            ServiceQueueZApiInstance.zapi_instance_id == zapi_instance_id,
            ServiceQueue.active.is_(True),
        )
        .order_by(ServiceQueue.is_default_inbound.desc(), ServiceQueue.id.asc())
    )
    return db.scalar(stmt.limit(1))


def instance_webhook_url(instance: ZApiInstance) -> str:
    if not settings.app_public_url:
        return ""
    return f"{settings.app_public_url}/webhooks/zapi/instance/{instance.webhook_key}"


def create_zapi_instance(
    db: Session,
    *,
    name: str,
    phone_label: str,
    base_url: str,
    instance_id: str,
    instance_token: str,
    client_token: str,
    created_by_id: int | None,
    source_type: str = "DB",
) -> ZApiInstance:
    item = ZApiInstance(
        name=name.strip(),
        phone_label=phone_label.strip(),
        base_url=(base_url.strip() or settings.zapi_base_url).rstrip("/"),
        instance_id=instance_id.strip(),
        instance_token_encrypted=encrypt_secret(instance_token),
        client_token_encrypted=encrypt_secret(client_token),
        webhook_key=secrets.token_urlsafe(36),
        source_type=source_type,
        active=True,
        created_by_id=created_by_id,
    )
    db.add(item)
    db.flush()
    return item


def seed_default_zapi_instance(
    db: Session,
    *,
    created_by_id: int | None,
) -> ZApiInstance | None:
    if not settings.integrations["zapi"]:
        return None

    item = db.scalar(
        select(ZApiInstance).where(
            ZApiInstance.instance_id == settings.zapi_instance_id
        )
    )
    if not item:
        item = create_zapi_instance(
            db,
            name="WhatsApp principal",
            phone_label="Instancia atual",
            base_url=settings.zapi_base_url,
            instance_id=settings.zapi_instance_id,
            instance_token=settings.zapi_instance_token,
            client_token=settings.zapi_client_token,
            created_by_id=created_by_id,
            source_type="ENV",
        )
    else:
        item.base_url = settings.zapi_base_url.rstrip("/")
        item.instance_token_encrypted = encrypt_secret(settings.zapi_instance_token)
        item.client_token_encrypted = encrypt_secret(settings.zapi_client_token)
        item.source_type = "ENV"
        item.active = True

    queue_ids = list(db.scalars(select(ServiceQueue.id)))
    bound_queue_ids = set(db.scalars(select(ServiceQueueZApiInstance.queue_id)))
    for queue_id in queue_ids:
        if queue_id not in bound_queue_ids:
            bind_queue_instance(db, queue_id, item.id)
    db.flush()
    return item
