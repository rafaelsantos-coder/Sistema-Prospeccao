from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import (
    Activity,
    ConversationAssignment,
    ConversationZApiInstance,
    Lead,
    Message,
    ServiceQueue,
    ServiceQueueZApiInstance,
    ZApiInstance,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.conversation_engine import (
    ensure_contact,
    ensure_open_conversation,
    payload_from_me,
    payload_message_content,
    register_message,
    store_received_payload,
    update_message_statuses,
)
from app.services.followups import next_followup_after
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.metrics import apply_user_scope
from app.services.queue_routing import (
    available_queues_for_user,
    get_queue,
    is_queue_member,
    set_lead_queue_preference,
)
from app.services.scoring import normalize_phone
from app.services.secret_store import encrypt_secret
from app.services.whatsapp_presence import is_user_whatsapp_online
from app.services.zapi import ZApiError, ZApiService
from app.services.zapi_instances import (
    create_zapi_instance,
    default_queue_for_instance,
    format_device_phone,
    get_zapi_instance,
    instance_by_webhook_key,
    instance_webhook_url,
    list_zapi_instances,
    queue_instance,
    refresh_instance_device_info,
    service_for_instance,
    service_for_queue,
)
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter()
LOG = logging.getLogger("uvicorn.error")
OPT_OUT_TERMS = (
    "pare de me chamar",
    "pare de mandar",
    "parar de mandar",
    "nao me chame",
    "não me chame",
    "nao me contate",
    "não me contate",
    "nao entre em contato",
    "não entre em contato",
    "nao quero mais mensagens",
    "não quero mais mensagens",
    "nao quero mais contato",
    "não quero mais contato",
    "remover meu numero",
    "remover meu número",
    "me remova",
    "descadastrar",
    "sair da lista",
)


def _is_explicit_opt_out(value: str | None) -> bool:
    text = (value or "").casefold().strip()
    return any(term in text for term in OPT_OUT_TERMS)


def _current_user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _lead_by_phone(db: Session, phone: str | None) -> Lead | None:
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    lead = db.scalar(select(Lead).where(Lead.phone == normalized))
    if lead:
        return lead
    suffix = normalized[-10:]
    return db.scalar(select(Lead).where(Lead.phone.endswith(suffix)).limit(1))


def _extract_message(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, dict) and text.get("message"):
        return str(text["message"]).strip()
    for key, field in (
        ("image", "caption"),
        ("video", "caption"),
        ("document", "fileName"),
        ("document", "title"),
        ("buttonsResponseMessage", "message"),
        ("listResponseMessage", "message"),
    ):
        value = payload.get(key)
        if isinstance(value, dict) and value.get(field):
            return str(value[field]).strip()
    if payload.get("audio"):
        return "[Audio recebido]"
    if payload.get("sticker"):
        return "[Sticker recebido]"
    if payload.get("location"):
        return "[Localizacao recebida]"
    if payload.get("contact"):
        return "[Contato recebido]"
    return ""


def _provider_message_id(payload: dict[str, Any]) -> str:
    for key in ("messageId", "zaapId", "id"):
        if payload.get(key):
            return str(payload[key])
    return ""


def _archive_message_media(message_id: int) -> None:
    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if not message or not message.media_url:
            return
        if not message.media_url.startswith(("http://", "https://")):
            return
        if message.message_type not in {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}:
            return
        filename = message.body if message.message_type == "DOCUMENT" else None
        try:
            archived_uri, _ = MediaStorage().archive_remote(
                url=message.media_url,
                conversation_id=message.conversation_id,
                filename=filename,
                mime_type=message.mime_type,
                direction=message.direction,
            )
        except MediaStorageError:
            return
        if archived_uri != message.media_url:
            message.media_url = archived_uri
            db.commit()
    finally:
        db.close()


def _process_zapi_payload(
    payload: dict[str, Any],
    *,
    db: Session,
    background_tasks: BackgroundTasks,
    expected_instance_id: str | None = None,
    queue_id: int | None = None,
    zapi_instance_id: int | None = None,
):
    if not isinstance(payload, dict):
        LOG.info("zapi_callback ignored=payload_not_dict instance_db=%s", zapi_instance_id)
        return {"ok": True, "ignored": "payload"}

    raw_type = str(payload.get("type") or "")
    raw_from_me = payload.get("fromMe")
    parsed_from_me = payload_from_me(payload)
    detected_type, _detected_body, _media_url, _mime_type = payload_message_content(payload)
    LOG.info(
        "zapi_callback instance_db=%s queue=%s callback_type=%s has_ids=%s has_phone=%s "
        "from_me_raw_type=%s from_me=%s message_type=%s has_text=%s group=%s newsletter=%s",
        zapi_instance_id,
        queue_id,
        raw_type or "-",
        bool(payload.get("ids")),
        bool(payload.get("phone") or payload.get("participantPhone")),
        type(raw_from_me).__name__,
        parsed_from_me,
        detected_type,
        detected_type != "UNKNOWN",
        bool(payload.get("isGroup")),
        bool(payload.get("isNewsletter")),
    )

    if expected_instance_id and payload.get("instanceId") not in {
        None,
        expected_instance_id,
    }:
        LOG.info("zapi_callback ignored=instance_mismatch instance_db=%s queue=%s", zapi_instance_id, queue_id)
        return {"ok": True, "ignored": "instance"}
    if payload.get("isGroup") or payload.get("isNewsletter"):
        LOG.info("zapi_callback ignored=non_direct instance_db=%s queue=%s", zapi_instance_id, queue_id)
        return {"ok": True, "ignored": "non_direct"}

    # Eventos de conexao sao notificacoes do aparelho, nao mensagens.
    # Atualizar o numero e registrar o estado evita perder a confirmacao
    # quando o endpoint /status da Z-API estiver temporariamente indisponivel.
    if raw_type in {"ConnectedCallback", "DisconnectedCallback"}:
        connected = raw_type == "ConnectedCallback"
        # Tipo de callback determina o evento; campos opcionais nao sao
        # exigidos, mas valores explicitamente contraditorios sao rejeitados.
        if connected and payload.get("connected") is False:
            return {"ok": True, "ignored": "invalid_connection_event"}
        if not connected and payload.get("disconnected") is False:
            return {"ok": True, "ignored": "invalid_connection_event"}
        if zapi_instance_id is None:
            return {"ok": True, "ignored": "unbound_connection_event"}
        instance = db.get(ZApiInstance, zapi_instance_id)
        if instance is None:
            return {"ok": True, "ignored": "unknown_instance"}
        if connected:
            digits = "".join(ch for ch in str(payload.get("phone") or "") if ch.isdigit())
            if digits.startswith("55") and len(digits) in {12, 13}:
                instance.phone_label = format_device_phone(digits)
        write_audit(
            db,
            user=None,
            action="ZAPI_CONNECTION_EVENT",
            entity_type="zapi_instance",
            entity_id=instance.id,
            payload={"connected": connected},
        )
        db.commit()
        LOG.info(
            "zapi_callback connection_event instance_db=%s queue=%s connected=%s",
            zapi_instance_id,
            queue_id,
            connected,
        )
        return {"ok": True, "connection_updated": True, "connected": connected}

    if payload.get("type") == "MessageStatusCallback" or payload.get("ids"):
        updated = update_message_statuses(db, payload)
        write_audit(
            db,
            user=None,
            action="ZAPI_MESSAGE_STATUS",
            entity_type="integration",
            entity_id=str(zapi_instance_id or "zapi"),
            payload={
                "status": payload.get("status"),
                "updated": updated,
                "zapi_instance_id": zapi_instance_id,
            },
        )
        db.commit()
        LOG.info(
            "zapi_callback status_only instance_db=%s queue=%s updated=%s status=%s",
            zapi_instance_id,
            queue_id,
            updated,
            payload.get("status"),
        )
        return {"ok": True, "status_updated": updated}

    conversation, message, created = store_received_payload(
        db,
        payload,
        queue_id=queue_id,
        zapi_instance_id=zapi_instance_id,
    )
    if not conversation or not message:
        db.commit()
        LOG.info(
            "zapi_callback ignored=unsupported_or_unmatched instance_db=%s queue=%s message_type=%s",
            zapi_instance_id,
            queue_id,
            detected_type,
        )
        return {"ok": True, "ignored": "unsupported_or_unmatched"}

    opt_out = False
    if created and message.direction == "INBOUND":
        opt_out = _is_explicit_opt_out(message.body)
        lead = conversation.lead
        if lead:
            now = utcnow()
            provider_id = message.provider_message_id or ""
            outcome = "RECEBIDA_ZAPI"
            if provider_id:
                outcome = f"{outcome}:{provider_id}"[:120]
            db.add(
                Activity(
                    lead_id=lead.id,
                    user_id=None,
                    kind="WHATSAPP",
                    status="CONCLUIDA",
                    channel="WHATSAPP",
                    outcome=outcome,
                    description="Mensagem recebida via Z-API; conteúdo disponível na conversa.",
                    completed_at=now,
                )
            )
            lead.last_contact_at = now
            lead.next_action_at = None
            if opt_out:
                lead.do_not_contact = True
                lead.stage = "NAO_CONTATAR"
            elif lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
                lead.stage = "CONTATO_REALIZADO"
        if opt_out:
            conversation.status = "BLOQUEADO"
            conversation.closed_at = utcnow()
        write_audit(
            db,
            user=None,
            action="CONVERSATION_MESSAGE_RECEIVED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "message_id": message.id,
                "provider_message_id": message.provider_message_id,
                "opt_out": opt_out,
                "zapi_instance_id": zapi_instance_id,
                "queue_id": queue_id,
            },
        )
    elif created:
        write_audit(
            db,
            user=None,
            action="CONVERSATION_EXTERNAL_OUTBOUND_CAPTURED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "message_id": message.id,
                "provider_message_id": message.provider_message_id,
                "zapi_instance_id": zapi_instance_id,
            },
        )
    db.commit()
    if (
        created
        and message.media_url
        and message.message_type in {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}
    ):
        background_tasks.add_task(_archive_message_media, message.id)
    LOG.info(
        "zapi_callback stored instance_db=%s queue=%s conversation=%s message=%s created=%s direction=%s message_type=%s",
        zapi_instance_id,
        queue_id,
        conversation.id,
        message.id,
        created,
        message.direction,
        message.message_type,
    )
    return {
        "ok": True,
        "conversation_id": conversation.id,
        "message_id": message.id,
        "created": created,
        "opt_out": opt_out,
        "zapi_instance_id": zapi_instance_id,
    }


@router.post("/webhooks/zapi/{webhook_secret}")
async def zapi_webhook(
    webhook_secret: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    expected = settings.zapi_webhook_secret
    if not expected or not secrets.compare_digest(webhook_secret, expected):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc
    return _process_zapi_payload(
        payload,
        db=db,
        background_tasks=background_tasks,
        expected_instance_id=settings.zapi_instance_id or None,
    )


@router.post("/webhooks/zapi/instance/{webhook_key}")
async def zapi_instance_webhook(
    webhook_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    instance = instance_by_webhook_key(db, webhook_key)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc
    default_queue = default_queue_for_instance(db, instance.id)
    return _process_zapi_payload(
        payload,
        db=db,
        background_tasks=background_tasks,
        expected_instance_id=instance.instance_id,
        queue_id=default_queue.id if default_queue else None,
        zapi_instance_id=instance.id,
    )


@router.post("/settings/zapi/configure-webhook")
def configure_zapi_webhook(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    if not settings.app_public_url:
        flash(request, "Defina APP_PUBLIC_URL no Railway.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)
    configured = 0
    errors: list[str] = []
    for instance in list_zapi_instances(db):
        url = instance_webhook_url(instance)
        try:
            service_for_instance(instance).configure_webhooks(url)
            configured += 1
        except ZApiError as exc:
            errors.append(f"{instance.name}: {exc}")
    if errors:
        flash(request, "Algumas instâncias falharam: " + " | ".join(errors[:3]), "error")
    else:
        flash(request, f"Webhooks atualizados em {configured} instância(s).")
    write_audit(
        db,
        user=user,
        action="ZAPI_WEBHOOKS_CONFIGURED",
        entity_type="integration",
        entity_id="zapi",
        payload={"configured": configured, "errors": errors},
    )
    db.commit()
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/settings/zapi/instances")
def create_zapi_instance_route(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    phone_label: str = Form(""),
    base_url: str = Form("https://api.z-api.io"),
    instance_id: str = Form(...),
    instance_token: str = Form(...),
    client_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    clean_instance_id = instance_id.strip()
    existing = db.scalar(
        select(ZApiInstance).where(ZApiInstance.instance_id == clean_instance_id)
    )
    if existing and (existing.active or existing.source_type == "ENV"):
        flash(request, "Esta instância Z-API já está cadastrada.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    # Instancias excluidas com historico sao arquivadas, nunca apagadas
    # fisicamente. Um novo cadastro do mesmo ID reativa o registro, com
    # credenciais novas e webhook rotacionado, preservando o historico.
    reactivated = existing is not None
    if existing:
        item = existing
        item.name = name.strip()
        item.phone_label = phone_label.strip()
        item.base_url = (base_url.strip() or settings.zapi_base_url).rstrip("/")
        item.instance_token_encrypted = encrypt_secret(instance_token)
        item.client_token_encrypted = encrypt_secret(client_token)
        item.webhook_key = secrets.token_urlsafe(36)
        item.active = True
    else:
        item = create_zapi_instance(
            db,
            name=name,
            phone_label=phone_label,
            base_url=base_url,
            instance_id=clean_instance_id,
            instance_token=instance_token,
            client_token=client_token,
            created_by_id=user.id,
        )
    # A validacao de credenciais e obrigatoria; os endpoints /device
    # e de webhook podem falhar temporariamente mesmo quando /status
    # valida as credenciais. Nesses casos o cadastro deve ser preservado,
    # com aviso explicito de que a integracao esta incompleta.
    device_info: dict[str, Any] = {}
    device_error: str | None = None
    webhook_error: str | None = None
    try:
        service = service_for_instance(item)
        status_result = service.status()
    except ZApiError as exc:
        db.rollback()
        reason = str(exc)
        if "client-token" in reason.casefold():
            flash(
                request,
                "A Z-API rejeitou o Client-Token. Copie o token de segurança "
                "da conta na área Segurança da Z-API; ele é diferente do "
                "Token da Instância. Verifique também se está ativado.",
                "error",
            )
        else:
            flash(
                request,
                f"Não foi possível validar as credenciais da instância na Z-API: {reason}",
                "error",
            )
        return RedirectResponse("/settings/whatsapp", status_code=303)
    if status_result.get("connected"):
        try:
            device_info = refresh_instance_device_info(item)
        except ZApiError as exc:
            device_error = str(exc)
    webhook_url = instance_webhook_url(item)
    if webhook_url:
        try:
            service.configure_webhooks(webhook_url)
        except ZApiError as exc:
            webhook_error = str(exc)
    else:
        webhook_error = "URL pública de webhook não configurada no Prospecta."

    write_audit(
        db,
        user=user,
        action="ZAPI_INSTANCE_REACTIVATED" if reactivated else "ZAPI_INSTANCE_CREATED",
        entity_type="zapi_instance",
        entity_id=item.id,
        payload={
            "name": item.name,
            "instance_id": item.instance_id,
            "phone_label": item.phone_label,
            "device_name": device_info.get("name"),
            "connected": bool(status_result.get("connected")),
            "webhook_configured": webhook_error is None,
            "device_lookup_ok": device_error is None,
        },
    )
    db.commit()
    verb = "reativada" if reactivated else "cadastrada"
    if webhook_error:
        hint = (
            " Verifique o Client-Token na área Segurança da Z-API."
            if "client-token" in webhook_error.casefold()
            else ""
        )
        flash(
            request,
            f"Instância {item.name} {verb}, mas o webhook não foi configurado: "
            f"{webhook_error}.{hint} Corrija a credencial e atualize o webhook "
            "antes de utilizar a fila para receber mensagens.",
            "warning",
        )
    elif device_error:
        flash(
            request,
            f"Instância {item.name} {verb} e webhook configurado. "
            "A consulta do telefone conectado falhou; revise o número no painel Z-API.",
            "warning",
        )
    elif not status_result.get("connected"):
        flash(
            request,
            f"Instância {item.name} {verb}, mas o WhatsApp não está conectado.",
            "warning",
        )
    else:
        flash(request, f"Instância {item.name} {verb} e webhook configurado.")
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.get("/settings/zapi/instances/{instance_db_id}/edit")
def edit_zapi_instance_page(
    instance_db_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR")
    instance = get_zapi_instance(db, instance_db_id)
    if not instance or not instance.active or instance.source_type == "ENV":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return render(
        request,
        "settings_zapi_edit.html",
        user=user,
        active_nav="settings",
        settings_section="whatsapp",
        page_title="Editar instância WhatsApp",
        instance=instance,
    )


@router.post("/settings/zapi/instances/{instance_db_id}/edit")
def update_zapi_instance_route(
    instance_db_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    instance_token: str = Form(""),
    client_token_mode: str = Form("keep"),
    client_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    instance = get_zapi_instance(db, instance_db_id)
    if not instance or not instance.active or instance.source_type == "ENV":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not name.strip() or client_token_mode not in {"keep", "replace", "remove"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if client_token_mode == "replace" and not client_token.strip():
        flash(request, "Informe o novo Client-Token ou selecione Manter atual.", "warning")
        return RedirectResponse(
            f"/settings/zapi/instances/{instance_db_id}/edit", status_code=303
        )

    instance.name = name.strip()
    if instance_token.strip():
        instance.instance_token_encrypted = encrypt_secret(instance_token)
    if client_token_mode == "replace":
        instance.client_token_encrypted = encrypt_secret(client_token)
    elif client_token_mode == "remove":
        instance.client_token_encrypted = ""
    try:
        service = service_for_instance(instance)
        connected = bool(service.status().get("connected"))
    except ZApiError as exc:
        db.rollback()
        error = str(exc)
        if "client-token" in error.casefold():
            error = (
                "A Z-API rejeitou o Client-Token. Confira a área Segurança da Z-API "
                "e a ativação do token de segurança da conta."
            )
        flash(request, f"Credenciais não validadas. Alterações descartadas: {error}", "error")
        return RedirectResponse(
            f"/settings/zapi/instances/{instance_db_id}/edit", status_code=303
        )

    device_error = None
    if connected:
        try:
            refresh_instance_device_info(instance)
        except ZApiError as exc:
            device_error = str(exc)

    webhook_error = None
    webhook_url = instance_webhook_url(instance)
    if webhook_url:
        try:
            service.configure_webhooks(webhook_url)
        except ZApiError as exc:
            webhook_error = str(exc)
    else:
        webhook_error = "URL pública da aplicação não configurada"

    write_audit(
        db,
        user=user,
        action="ZAPI_INSTANCE_UPDATED",
        entity_type="zapi_instance",
        entity_id=instance.id,
        payload={
            "name": instance.name,
            "connected": connected,
            "credentials_changed": bool(instance_token.strip() or client_token_mode != "keep"),
            "webhook_configured": webhook_error is None,
        },
    )
    db.commit()
    if webhook_error:
        flash(
            request,
            "Instância atualizada, mas o webhook continua pendente: "
            f"{webhook_error}. Não utilize esta fila para receber mensagens até corrigir.",
            "warning",
        )
    elif device_error:
        flash(
            request,
            f"Instância {instance.name} atualizada e webhook registrado. "
            "Não foi possível identificar o número conectado.",
            "warning",
        )
    else:
        flash(request, f"Instância {instance.name} atualizada e webhook registrado.")
    return RedirectResponse("/settings/whatsapp", status_code=303)


def _queues_bound_to_zapi_instance(db: Session, instance_db_id: int) -> list[ServiceQueue]:
    # Bloquear tambem filas inativas: o vinculo historico pode ser reativado.
    return list(
        db.scalars(
            select(ServiceQueue)
            .join(
                ServiceQueueZApiInstance,
                ServiceQueueZApiInstance.queue_id == ServiceQueue.id,
            )
            .where(ServiceQueueZApiInstance.zapi_instance_id == instance_db_id)
            .order_by(ServiceQueue.name)
        )
    )


@router.get("/settings/zapi/instances/{instance_db_id}/delete")
def confirm_delete_zapi_instance(
    instance_db_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR")
    instance = get_zapi_instance(db, instance_db_id)
    if not instance or not instance.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return render(
        request,
        "settings_zapi_delete.html",
        user=user,
        active_nav="settings",
        settings_section="whatsapp",
        page_title="Excluir instância WhatsApp",
        instance=instance,
        bound_queues=_queues_bound_to_zapi_instance(db, instance_db_id),
        replacement_instances=[
            item for item in list_zapi_instances(db)
            if item.id != instance_db_id
        ],
    )


@router.post("/settings/zapi/instances/{instance_db_id}/delete")
def delete_zapi_instance(
    instance_db_id: int,
    request: Request,
    csrf_token: str = Form(...),
    replacement_instance_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")

    instance = get_zapi_instance(db, instance_db_id)
    if not instance or not instance.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # A instancia principal do Railway e recriada no bootstrap;
    # excluir seu cadastro nao removia a origem real da configuracao.
    if instance.source_type == "ENV":
        flash(request, "Esta instância é gerenciada pelo Railway e não pode ser excluída aqui.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    # A exclusao pode reatribuir todas as filas vinculadas, mas SOMENTE
    # depois da escolha explicita de outra instancia pelo gestor.
    queues = _queues_bound_to_zapi_instance(db, instance_db_id)
    replacement = None
    if queues:
        if replacement_instance_id is None or replacement_instance_id == instance_db_id:
            flash(request, "Escolha outra instância WhatsApp para assumir as filas antes de excluir.", "warning")
            return RedirectResponse(
                f"/settings/zapi/instances/{instance_db_id}/delete", status_code=303
            )
        replacement = get_zapi_instance(db, replacement_instance_id)
        if not replacement or not replacement.active:
            flash(request, "A instância de substituição não está ativa ou não existe.", "warning")
            return RedirectResponse(
                f"/settings/zapi/instances/{instance_db_id}/delete", status_code=303
            )
        try:
            replacement_status = service_for_instance(replacement).status()
        except ZApiError:
            flash(
                request,
                "Não foi possível confirmar a conexão do WhatsApp substituto. Nenhuma fila foi alterada.",
                "warning",
            )
            return RedirectResponse(
                f"/settings/zapi/instances/{instance_db_id}/delete", status_code=303
            )
        if not replacement_status.get("connected"):
            flash(
                request,
                "O WhatsApp substituto está desconectado. Escolha uma instância conectada.",
                "warning",
            )
            return RedirectResponse(
                f"/settings/zapi/instances/{instance_db_id}/delete", status_code=303
            )

    # Travar os vinculos para que a migracao e a exclusao sejam atomicas.
    # A verificacao e repetida no POST, nao depende da tela aberta antes.
    linked_rows = list(
        db.scalars(
            select(ServiceQueueZApiInstance)
            .where(ServiceQueueZApiInstance.zapi_instance_id == instance_db_id)
            .with_for_update()
        )
    )
    moved_queue_ids: list[int] = []
    if linked_rows:
        if not replacement:
            flash(request, "Os vínculos mudaram. Atualize a página e escolha a instância substituta.", "warning")
            db.rollback()
            return RedirectResponse(
                f"/settings/zapi/instances/{instance_db_id}/delete", status_code=303
            )
        for link in linked_rows:
            moved_queue_ids.append(link.queue_id)
            link.zapi_instance_id = replacement.id
        db.flush()

    history_exists = db.scalar(
        select(ConversationZApiInstance.id)
        .where(ConversationZApiInstance.zapi_instance_id == instance_db_id)
        .limit(1)
    ) is not None
    name = instance.name
    if history_exists:
        # Preservar as referencias de conversas antigas e impedir novos
        # webhooks/envios usando este registro arquivado.
        instance.active = False
    else:
        db.delete(instance)
    write_audit(
        db,
        user=user,
        action="ZAPI_INSTANCE_DELETED",
        entity_type="zapi_instance",
        entity_id=instance_db_id,
        payload={
            "name": name,
            "historical_record_preserved": history_exists,
            "moved_queue_ids": moved_queue_ids,
            "replacement_instance_db_id": replacement.id if replacement else None,
        },
    )
    db.commit()
    if moved_queue_ids:
        flash(
            request,
            f"Instância {name} excluída. {len(moved_queue_ids)} fila(s) transferida(s) "
            f"para {replacement.name}. Histórico preservado.",
        )
    else:
        flash(request, f"Instância {name} excluída do cadastro. Histórico preservado.")
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/settings/zapi/instances/{instance_db_id}/configure-webhook")
def configure_instance_webhook(
    instance_db_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    instance = get_zapi_instance(db, instance_db_id)
    if not instance or not instance.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    url = instance_webhook_url(instance)
    if not url:
        flash(request, "Defina APP_PUBLIC_URL antes de configurar o webhook.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)
    try:
        service_for_instance(instance).configure_webhooks(url)
    except ZApiError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, f"Webhook da instância {instance.name} atualizado.")
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/settings/zapi/test")
def test_zapi_message(
    request: Request,
    csrf_token: str = Form(...),
    phone: str = Form(...),
    message: str = Form(...),
    zapi_instance_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    instance = get_zapi_instance(db, zapi_instance_id)
    if not instance or not instance.active:
        flash(
            request,
            "A instância selecionada não existe ou está inativa. Nenhuma mensagem foi enviada.",
            "error",
        )
        return RedirectResponse("/settings/whatsapp", status_code=303)

    service = service_for_instance(instance)
    device_info: dict[str, Any] = {}
    try:
        device_info = refresh_instance_device_info(instance)
        result = service.send_text(phone=phone, message=message)
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    duplicate = None
    if instance.phone_label:
        duplicate = db.scalar(
            select(ZApiInstance).where(
                ZApiInstance.id != instance.id,
                ZApiInstance.active.is_(True),
                ZApiInstance.phone_label == instance.phone_label,
            )
        )
    provider_id = _provider_message_id(result)
    write_audit(
        db,
        user=user,
        action="ZAPI_TEST_SENT",
        entity_type="integration",
        entity_id="zapi",
        payload={"provider_message_id": provider_id, "zapi_instance_id": instance.id if instance else None},
    )
    db.commit()
    source_number = (
        instance.phone_label
        or str(device_info.get("phone") or "")
        or "número não identificado"
    )
    if duplicate:
        flash(
            request,
            (
                f"Teste enviado pela instância {instance.name} · {source_number}. "
                f"Atenção: a instância {duplicate.name} está conectada ao mesmo número."
            ),
            "warning",
        )
    else:
        flash(
            request,
            f"Teste enviado pela instância {instance.name} · {source_number}.",
        )
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/leads/{lead_id}/whatsapp")
def send_lead_whatsapp(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    message: str = Form(...),
    queue_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    stmt = apply_user_scope(select(Lead).where(Lead.id == lead_id), user)
    lead = db.scalar(stmt)
    if not lead:
        flash(request, "Lead nao encontrado.", "error")
        return RedirectResponse("/leads", status_code=303)
    if lead.do_not_contact or lead.stage == "NAO_CONTATAR":
        flash(request, "Este lead esta bloqueado para novos contatos.", "warning")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    if not lead.phone:
        flash(request, "O lead nao possui telefone cadastrado.", "warning")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        flash(
            request,
            "Entre no WhatsApp antes de enviar uma mensagem individual.",
            "warning",
        )
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    service_queue = get_queue(db, queue_id)
    allowed_queue_ids = {item.id for item in available_queues_for_user(db, user)}
    if not service_queue or service_queue.id not in allowed_queue_ids:
        flash(request, "Selecione uma fila de atendimento válida para este envio.", "warning")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    clean_message = message.strip()
    instance = queue_instance(db, service_queue.id)
    try:
        result = service_for_queue(db, service_queue.id).send_text(
            phone=lead.phone,
            message=clean_message,
        )
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)

    try:
        contact = ensure_contact(db, lead.phone, lead=lead)
    except ValueError:
        flash(request, "Telefone invalido para o WhatsApp.", "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    set_lead_queue_preference(
        db,
        lead.id,
        service_queue.id,
        source="MANUAL_SEND",
    )
    conversation = ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source=lead.source,
        queue_id=service_queue.id,
        queue_origin="MANUAL_SEND",
        assign_if_unowned=False,
        zapi_instance_id=instance.id if instance else None,
    )
    if (
        user.role == "CONSULTOR"
        and conversation.assigned_user_id is None
        and is_user_whatsapp_online(db, user.id)
        and is_queue_member(db, service_queue.id, user.id)
    ):
        conversation.assigned_user_id = user.id
        conversation.status = "ATRIBUIDO"
        lead.assigned_user_id = user.id
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=user.id,
                assigned_by_id=user.id,
                event="CLAIMED_QUEUE",
            )
        )
    now = utcnow()
    provider_id = _provider_message_id(result)
    zaap_id = str(result.get("zaapId") or "").strip() or None
    stored = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=clean_message,
        sender_user_id=user.id,
        provider_message_id=provider_id or None,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    outcome = "ENVIADA_ZAPI"
    if provider_id:
        outcome = f"{outcome}:{provider_id}"[:120]
    db.add(
        Activity(
            lead_id=lead.id,
            user_id=user.id,
            kind="WHATSAPP",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=outcome,
            description=f"Mensagem enviada via Z-API: {clean_message}",
            completed_at=now,
        )
    )
    lead.last_contact_at = now
    lead.next_action_at = next_followup_after(now, 0)
    if lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
        lead.stage = "CONTATO_REALIZADO"
    write_audit(
        db,
        user=user,
        action="ZAPI_MESSAGE_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "provider_message_id": provider_id,
            "message_id": stored.id,
            "queue_id": service_queue.id,
            "zapi_instance_id": instance.id if instance else None,
        },
    )
    db.commit()
    flash(request, "Mensagem enviada e registrada na conversa.")
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.get("/api/integrations/zapi/status")
def zapi_status_api(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return {"error": "unauthorized"}
    try:
        return ZApiService().status()
    except ZApiError:
        LOG.exception("zapi_status_failed")
        return {
            "configured": settings.integrations["zapi"],
            "connected": False,
            "error": "Não foi possível consultar o status da Z-API.",
        }
