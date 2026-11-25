from __future__ import annotations

import os
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConversationZApiInstance,
    ServiceQueue,
    ServiceQueueMetaAccount,
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
    # Substituicao isolada do Client-Token por instancia, via segredo Railway.
    override_name = f"ZAPI_CLIENT_TOKEN_OVERRIDE_{instance.instance_id.strip().upper()}"
    override_token = os.getenv(override_name, "").strip()
    return ZApiService(
        base_url=instance.base_url,
        instance_id=instance.instance_id,
        instance_token=decrypt_secret(instance.instance_token_encrypted),
        client_token=override_token or decrypt_secret(instance.client_token_encrypted),
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
    # Uma fila tem um único canal WhatsApp: Meta e Z-API são mutuamente exclusivos.
    from app.services.meta_whatsapp import clear_queue_meta_binding

    clear_queue_meta_binding(db, queue_id)
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


def _service_for_queue_connection(db: Session, queue_id: int):
    """Resolve Z-API ou Meta a partir do vínculo atual da fila."""
    from app.services.meta_whatsapp import queue_meta_account, service_for_meta_account

    instance = queue_instance(db, queue_id)
    if instance:
        return service_for_instance(instance)
    meta = queue_meta_account(db, queue_id)
    if meta:
        return service_for_meta_account(meta)
    raise ZApiNotConfigured(
        "A fila selecionada não possui uma conexão WhatsApp ativa configurada."
    )


def service_for_queue(db: Session, queue_id: int | None) -> ZApiService:
    if queue_id:
        return _service_for_queue_connection(db, queue_id)  # type: ignore[return-value]
    return ZApiService()


def service_for_conversation(db: Session, conversation_id: int) -> ZApiService:
    """
    A fila é a fonte oficial da instância de saída.

    O vínculo salvo na conversa serve para histórico/entrada, mas nunca pode
    sobrescrever o número configurado atualmente na fila. Se estiver antigo,
    ele é autocorrigido antes do envio.
    """
    from app.models import Conversation, ConversationMetaAccount
    from app.services.meta_whatsapp import (
        bind_conversation_meta_account,
        conversation_meta_account,
        default_queue_for_meta_account,
        queue_meta_account,
        service_for_meta_account,
    )
    from app.services.queue_routing import manually_transferred_to_ai, queue_for_conversation

    conversation = db.get(Conversation, conversation_id)
    if conversation is not None and conversation.source == "TELEGRAM":
        # Conversa do canal Telegram: mesma interface de envio, outro remetente.
        from app.services.telegram import service_for_telegram_conversation

        return service_for_telegram_conversation(db, conversation)  # type: ignore[return-value]

    # Uma conversa Meta mantém o número de origem mesmo após alterações de fila.
    # Se a conta estiver inativa, vinculada a outra fila ou misturada com Z-API,
    # bloquear o envio em vez de sair por outro WhatsApp.
    meta_link = db.scalar(select(ConversationMetaAccount).where(
        ConversationMetaAccount.conversation_id == conversation_id
    ))
    if meta_link:
        meta = conversation_meta_account(db, conversation_id)
        queue = queue_for_conversation(db, conversation_id)
        bound = default_queue_for_meta_account(db, meta.id) if meta else None
        if (not meta or not queue or not bound or bound.id != queue.id
                or queue_instance(db, queue.id)
                or conversation_instance(db, conversation_id)):
            raise ZApiNotConfigured("Vínculo Meta indisponível ou inconsistente; envio bloqueado.")
        return service_for_meta_account(meta)  # type: ignore[return-value]

    queue = queue_for_conversation(db, conversation_id)
    if queue and queue_meta_account(db, queue.id) and conversation_instance(db, conversation_id):
        raise ZApiNotConfigured("Conversa Z-API não pode utilizar um número oficial Meta.")

    if manually_transferred_to_ai(db, conversation_id):
        # Delegar para a SulIA nao troca a fila/linha WhatsApp: a instancia
        # da fila original prevalece, mesmo que a Fila IA esteja desconectada.
        from app.services.ai_queue_scope import is_dedicated_ai_queue  # noqa: PLC0415

        queue = queue_for_conversation(db, conversation_id)
        original_instance = conversation_instance(db, conversation_id)
        original_meta = conversation_meta_account(db, conversation_id)
        if is_dedicated_ai_queue(queue):
            # Compatibilidade com transferencias antigas: a conversa foi MOVIDA
            # para Fila IA, mas o numero original esta gravado na conversa.
            instance = original_instance or (queue_instance(db, queue.id) if queue else None)
            meta = original_meta or (queue_meta_account(db, queue.id) if queue else None)
        else:
            # Novo fluxo: a conversa nunca sai da fila de origem.
            instance = (queue_instance(db, queue.id) if queue else None) or original_instance
            meta = (queue_meta_account(db, queue.id) if queue else None) or original_meta
        if instance:
            current = conversation_instance(db, conversation_id)
            if not current or current.id != instance.id:
                bind_conversation_instance(db, conversation_id, instance.id)
            return service_for_instance(instance)
        if meta:
            current_meta = conversation_meta_account(db, conversation_id)
            if not current_meta or current_meta.id != meta.id:
                bind_conversation_meta_account(db, conversation_id, meta.id)
            return service_for_meta_account(meta)  # type: ignore[return-value]
        raise ZApiNotConfigured("A conversa delegada à IA está sem a conexão WhatsApp de origem.")

    queue = queue_for_conversation(db, conversation_id)
    if queue:
        instance = queue_instance(db, queue.id)
        if instance:
            current = conversation_instance(db, conversation_id)
            if not current or current.id != instance.id:
                bind_conversation_instance(db, conversation_id, instance.id)
            return service_for_instance(instance)
        meta = queue_meta_account(db, queue.id)
        if meta:
            current_meta = conversation_meta_account(db, conversation_id)
            if not current_meta or current_meta.id != meta.id:
                bind_conversation_meta_account(db, conversation_id, meta.id)
            return service_for_meta_account(meta)  # type: ignore[return-value]
        raise ZApiNotConfigured(
            f"A fila {queue.name} não possui uma conexão WhatsApp ativa configurada."
        )

    instance = conversation_instance(db, conversation_id)
    if instance:
        return service_for_instance(instance)
    meta = conversation_meta_account(db, conversation_id)
    if meta:
        return service_for_meta_account(meta)  # type: ignore[return-value]
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
    # A fila Meta já possui provedor, mesmo sem vínculo Z-API.
    # Nunca substituir essa configuração durante o bootstrap/redeploy.
    bound_queue_ids = set(db.scalars(select(ServiceQueueZApiInstance.queue_id)))
    bound_queue_ids.update(db.scalars(select(ServiceQueueMetaAccount.queue_id)))
    for queue_id in queue_ids:
        if queue_id not in bound_queue_ids:
            bind_queue_instance(db, queue_id, item.id)
    db.flush()
    return item
