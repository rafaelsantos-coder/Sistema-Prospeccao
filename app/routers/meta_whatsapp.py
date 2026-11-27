"""Cadastro de números WhatsApp (Cloud API oficial Meta) e webhooks."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import MetaWhatsAppAccount, ServiceQueueMetaAccount
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.conversation_engine import (
    store_received_payload,
    update_message_statuses,
)
from app.services.meta_whatsapp import (
    account_by_webhook_key,
    account_webhook_url,
    create_meta_account,
    default_queue_for_meta_account,
    format_display_phone,
    list_meta_accounts,
    meta_inbound_to_engine_payload,
    meta_status_to_engine_payload,
    service_for_meta_account,
    verify_webhook_signature,
)
from app.services.secret_store import decrypt_secret, encrypt_secret
from app.services.meta_images import archive_meta_image_task
from app.services.zapi import ZApiError
from app.web import flash

router = APIRouter()
LOG = logging.getLogger("uvicorn.error")


def _current_user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _process_meta_value(
    value: dict[str, Any],
    *,
    db: Session,
    queue_id: int | None,
    meta_account_id: int | None,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    contacts = value.get("contacts") if isinstance(value.get("contacts"), list) else []
    wa_by_id: dict[str, str] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        wa_id = str(contact.get("wa_id") or "").strip()
        if wa_id:
            wa_by_id[wa_id] = wa_id

    processed = 0
    messages = value.get("messages") if isinstance(value.get("messages"), list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = str(message.get("from") or "").strip()
        contact_wa_id = wa_by_id.get(sender) or sender
        if not contact_wa_id:
            continue
        engine_payload = meta_inbound_to_engine_payload(
            message, contact_wa_id=contact_wa_id
        )
        if not queue_id or not meta_account_id:
            LOG.warning("meta_inbound_blocked account_id=%s reason=invalid_queue", meta_account_id)
            raise HTTPException(status_code=503, detail="Fila Meta indisponível")
        conversation, stored, created = store_received_payload(
            db,
            engine_payload,
            queue_id=queue_id,
            meta_account_id=meta_account_id,
        )
        if stored and created and conversation:
            from app.services.queue_routing import queue_for_conversation
            actual = queue_for_conversation(db, conversation.id)
            if not actual or actual.id != queue_id:
                db.rollback()
                raise HTTPException(status_code=503, detail="Fila Meta divergente")
            db.commit()
            if stored.message_type == "IMAGE" and background_tasks is not None:
                background_tasks.add_task(archive_meta_image_task, stored.id)
            LOG.info("meta_inbound_routed account_id=%s conversation_id=%s queue_id=%s queue_name=%s", meta_account_id, conversation.id, actual.id, actual.name)
            processed += 1

    statuses = value.get("statuses") if isinstance(value.get("statuses"), list) else []
    for status_item in statuses:
        if not isinstance(status_item, dict):
            continue
        engine_payload = meta_status_to_engine_payload(status_item)
        message_id = str(engine_payload.get("messageId") or "").strip()
        status_value = str(engine_payload.get("status") or "").strip()
        if message_id and status_value:
            update_message_statuses(db, engine_payload)
            processed += 1

    db.commit()
    return {"ok": True, "processed": processed}


@router.get("/webhooks/meta/{webhook_key}")
def meta_webhook_verify(
    webhook_key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    account = account_by_webhook_key(db, webhook_key)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token and secrets.compare_digest(token, account.verify_token):
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhooks/meta/{webhook_key}")
async def meta_webhook_receive(
    webhook_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    account = account_by_webhook_key(db, webhook_key)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    app_secret = (
        decrypt_secret(account.app_secret_encrypted)
        if account.app_secret_encrypted
        else ""
    )
    if app_secret and not verify_webhook_signature(
        app_secret=app_secret, body=body, signature_header=signature
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {"ok": True, "ignored": "invalid_json"}

    if not isinstance(payload, dict):
        return {"ok": True, "ignored": "payload"}

    default_queue = default_queue_for_meta_account(db, account.id)
    queue_id = default_queue.id if default_queue else None
    entries = payload.get("entry") if isinstance(payload.get("entry"), list) else []
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes") if isinstance(entry.get("changes"), list) else []
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            if phone_number_id and phone_number_id != account.phone_number_id:
                LOG.info("meta_webhook ignored=phone_mismatch")
                continue
            result = _process_meta_value(
                value,
                db=db,
                queue_id=queue_id,
                meta_account_id=account.id,
                background_tasks=background_tasks,
            )
            total += int(result.get("processed") or 0)
    return {"ok": True, "processed": total}


@router.post("/settings/meta/accounts")
def create_meta_account_route(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    phone_label: str = Form(""),
    phone_number_id: str = Form(...),
    waba_id: str = Form(""),
    access_token: str = Form(...),
    app_secret: str = Form(""),
    verify_token: str = Form(""),
    graph_api_version: str = Form("v21.0"),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")

    clean_phone_number_id = phone_number_id.strip()
    if not clean_phone_number_id or not access_token.strip():
        flash(request, "Informe o Phone Number ID e o token de acesso da Meta.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    existing = db.scalar(
        select(MetaWhatsAppAccount).where(
            MetaWhatsAppAccount.phone_number_id == clean_phone_number_id
        )
    )
    if existing and existing.active:
        flash(request, "Este Phone Number ID já está cadastrado.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    reactivated = existing is not None
    if existing:
        item = existing
        item.name = name.strip()
        item.phone_label = phone_label.strip()
        item.waba_id = waba_id.strip()
        item.access_token_encrypted = encrypt_secret(access_token)
        item.app_secret_encrypted = (
            encrypt_secret(app_secret) if app_secret.strip() else item.app_secret_encrypted
        )
        if verify_token.strip():
            item.verify_token = verify_token.strip()
        item.graph_api_version = (graph_api_version.strip() or "v21.0")
        item.webhook_key = secrets.token_urlsafe(36)
        item.active = True
    else:
        item = create_meta_account(
            db,
            name=name,
            phone_label=phone_label,
            phone_number_id=clean_phone_number_id,
            waba_id=waba_id,
            access_token=access_token,
            app_secret=app_secret,
            verify_token=verify_token,
            graph_api_version=graph_api_version,
            created_by_id=user.id,
        )

    try:
        service = service_for_meta_account(item)
        info = service.phone_info()
    except ZApiError as exc:
        db.rollback()
        flash(
            request,
            f"Não foi possível validar o número na API oficial do Meta: {exc}",
            "error",
        )
        return RedirectResponse("/settings/whatsapp", status_code=303)

    display = format_display_phone(info.get("display_phone_number"))
    if display:
        item.phone_label = display
    elif info.get("verified_name") and not item.phone_label:
        item.phone_label = str(info["verified_name"])

    write_audit(
        db,
        user=user,
        action="META_WA_ACCOUNT_REACTIVATED" if reactivated else "META_WA_ACCOUNT_CREATED",
        entity_type="meta_whatsapp_account",
        entity_id=item.id,
        payload={
            "name": item.name,
            "phone_number_id": item.phone_number_id,
            "phone_label": item.phone_label,
            "verified_name": info.get("verified_name"),
            "webhook_url": account_webhook_url(item),
        },
    )
    db.commit()
    verb = "reativado" if reactivated else "cadastrado"
    webhook = account_webhook_url(item)
    if webhook:
        flash(
            request,
            f"Número Meta {item.name} {verb}. Configure o webhook no painel da Meta com a URL "
            f"e o verify token exibidos abaixo na lista.",
        )
    else:
        flash(
            request,
            f"Número Meta {item.name} {verb}, mas APP_PUBLIC_URL não está definido — "
            "configure a URL pública antes de apontar o webhook na Meta.",
            "warning",
        )
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/settings/meta/accounts/{account_id}/deactivate")
def deactivate_meta_account(
    account_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")

    account = db.get(MetaWhatsAppAccount, account_id)
    if not account or not account.active:
        flash(request, "Número Meta não encontrado.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    bound = db.scalar(
        select(ServiceQueueMetaAccount.id).where(
            ServiceQueueMetaAccount.meta_account_id == account.id
        )
    )
    if bound:
        flash(
            request,
            "Este número ainda está vinculado a uma fila. Desvincule-o antes de desativar.",
            "warning",
        )
        return RedirectResponse("/settings/whatsapp", status_code=303)

    account.active = False
    write_audit(
        db,
        user=user,
        action="META_WA_ACCOUNT_DEACTIVATED",
        entity_type="meta_whatsapp_account",
        entity_id=account.id,
        payload={"name": account.name, "phone_number_id": account.phone_number_id},
    )
    db.commit()
    flash(request, f"Número Meta {account.name} desativado.")
    return RedirectResponse("/settings/whatsapp", status_code=303)


@router.post("/settings/meta/test")
def test_meta_send(
    request: Request,
    csrf_token: str = Form(...),
    meta_account_id: int = Form(...),
    phone: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")

    account = db.get(MetaWhatsAppAccount, meta_account_id)
    if not account or not account.active:
        flash(request, "Selecione um número Meta ativo.", "warning")
        return RedirectResponse("/settings/whatsapp", status_code=303)
    try:
        result = service_for_meta_account(account).send_text(phone=phone, message=message)
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/whatsapp", status_code=303)

    write_audit(
        db,
        user=user,
        action="META_WA_TEST_SENT",
        entity_type="meta_whatsapp_account",
        entity_id=account.id,
        payload={"provider_message_id": result.get("messageId"), "phone": phone},
    )
    db.commit()
    flash(request, f"Mensagem de teste enviada pelo número Meta {account.name}.")
    return RedirectResponse("/settings/whatsapp", status_code=303)
