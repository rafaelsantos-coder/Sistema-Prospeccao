"""API oficial do WhatsApp (Meta Cloud API): webhook e configuração.

O webhook converte o formato da Meta para o mesmo dicionário que a Z-API
entrega e reaproveita ``_process_zapi_payload``. Assim opt-out, atividades do
lead, distribuição na fila, Vendedor IA e status de mensagens funcionam
igual para os dois provedores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, RedirectResponse

from app.database import SessionLocal, get_db
from app.models import Campaign, Message, MetaMessageTemplate, MetaWhatsAppNumber
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.audio_transcription import (
    TRANSCRIBED_PREFIX,
    UNREADABLE_BODY,
    transcribe_audio_bytes,
)
from app.services.campaign_queue import campaign_message_template, set_campaign_meta_template
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.meta_cloud import MetaCloudError
from app.services.meta_numbers import (
    bind_queue_meta_number,
    create_meta_number,
    default_queue_for_meta_number,
    get_meta_number,
    list_meta_numbers,
    meta_app_secret,
    meta_number_by_phone_number_id,
    meta_verify_token,
    meta_webhook_url,
    refresh_meta_number_info,
    service_for_meta_number,
    sync_meta_templates,
    template_unsupported_reason,
    template_variables,
    unbind_queue_meta_number,
    whatsapp_id_to_phone,
)
from app.services.queue_routing import get_queue
from app.web import flash


router = APIRouter()
LOG = logging.getLogger("uvicorn.error")
PROVIDER_LABEL = "API oficial da Meta"
MEDIA_TYPES = {"image": "IMAGE", "video": "VIDEO", "document": "DOCUMENT", "sticker": "STICKER"}
STATUS_MAP = {"sent": "SENT", "delivered": "RECEIVED", "read": "READ", "failed": "FAILED"}


def _current_user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


# ------------------------------------------------------------------ webhook


def _signature_valid(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not app_secret:
        return False
    algo, _, received = header.partition("=")
    if algo.strip().lower() != "sha256" or not received:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


def _changes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if isinstance(change, dict) and change.get("field") == "messages":
                value = change.get("value")
                if isinstance(value, dict):
                    items.append(value)
    return items


def _phone_number_id(value: dict[str, Any]) -> str:
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("phone_number_id") or "")
    return ""


def _sender_names(value: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        profile = contact.get("profile") if isinstance(contact.get("profile"), dict) else {}
        wa_id = str(contact.get("wa_id") or "")
        if wa_id:
            names[wa_id] = str(profile.get("name") or "")
    return names


def meta_message_to_internal(
    message: dict[str, Any],
    *,
    sender_name: str = "",
    audio_text: str | None = None,
) -> dict[str, Any] | None:
    """Mensagem recebida da Meta no formato que ``store_received_payload`` entende.

    Mídia chega sem URL (a Meta só entrega um ID que exige token). O arquivo é
    baixado depois, em segundo plano, e anexado à mensagem.
    """
    wa_id = str(message.get("from") or "")
    message_id = str(message.get("id") or "")
    kind = str(message.get("type") or "")
    if not wa_id or not message_id:
        return None
    internal: dict[str, Any] = {
        "phone": whatsapp_id_to_phone(wa_id),
        "messageId": message_id,
        "fromMe": False,
        "senderName": sender_name,
        "provider": "META",
    }
    if kind == "text":
        body = message.get("text") if isinstance(message.get("text"), dict) else {}
        internal["text"] = {"message": str(body.get("body") or "")}
    elif kind == "interactive":
        interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if not isinstance(reply, dict):
            return None
        if interactive.get("list_reply"):
            internal["listResponseMessage"] = {
                "message": str(reply.get("title") or ""),
                "selectedRowId": str(reply.get("id") or ""),
            }
        else:
            internal["buttonsResponseMessage"] = {
                "message": str(reply.get("title") or ""),
                "buttonId": str(reply.get("id") or ""),
            }
    elif kind == "button":
        # Resposta a botão de resposta rápida de um template.
        button = message.get("button") if isinstance(message.get("button"), dict) else {}
        internal["buttonsResponseMessage"] = {
            "message": str(button.get("text") or button.get("payload") or ""),
            "buttonId": str(button.get("payload") or ""),
        }
    elif kind == "audio":
        text = audio_text if audio_text is not None else ""
        internal["text"] = {"message": f"{TRANSCRIBED_PREFIX} {text}" if text else UNREADABLE_BODY}
    elif kind in MEDIA_TYPES:
        media = message.get(kind) if isinstance(message.get(kind), dict) else {}
        key = {"image": "imageUrl", "video": "videoUrl", "document": "documentUrl", "sticker": "stickerUrl"}[kind]
        data: dict[str, Any] = {key: None, "mimeType": media.get("mime_type")}
        if kind in {"image", "video"}:
            data["caption"] = str(media.get("caption") or "")
        if kind == "document":
            data["fileName"] = str(media.get("filename") or "")
        internal[kind] = data
    elif kind == "location":
        location = message.get("location") if isinstance(message.get("location"), dict) else {}
        lat, lng = location.get("latitude"), location.get("longitude")
        internal["location"] = {
            "name": str(location.get("name") or ""),
            "address": str(location.get("address") or ""),
            "url": f"https://maps.google.com/?q={lat},{lng}" if lat is not None and lng is not None else None,
        }
    elif kind == "contacts":
        contacts = message.get("contacts") if isinstance(message.get("contacts"), list) else []
        first = contacts[0] if contacts and isinstance(contacts[0], dict) else {}
        name = first.get("name") if isinstance(first.get("name"), dict) else {}
        internal["contact"] = {"displayName": str(name.get("formatted_name") or "[Contato]")}
    elif kind == "reaction":
        reaction = message.get("reaction") if isinstance(message.get("reaction"), dict) else {}
        internal["reaction"] = {"value": str(reaction.get("emoji") or "")}
    else:
        return None
    return internal


def meta_status_to_internal(item: dict[str, Any]) -> dict[str, Any] | None:
    message_id = str(item.get("id") or "")
    mapped = STATUS_MAP.get(str(item.get("status") or "").lower())
    if not message_id or not mapped:
        return None
    payload: dict[str, Any] = {
        "type": "MessageStatusCallback",
        "status": mapped,
        "ids": [message_id],
        "provider": "META",
    }
    errors = item.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        payload["error"] = {
            "code": errors[0].get("code"),
            "title": str(errors[0].get("title") or "")[:240],
        }
    return payload


def _transcribe_meta_audio(number: MetaWhatsAppNumber, message: dict[str, Any]) -> str:
    audio = message.get("audio") if isinstance(message.get("audio"), dict) else {}
    media_id = str(audio.get("id") or "")
    if not media_id:
        return ""
    try:
        data, mime = service_for_meta_number(number).download_media(media_id)
    except MetaCloudError as exc:
        LOG.warning("meta_audio_download_failed number=%s error=%s", number.id, type(exc).__name__)
        return ""
    return transcribe_audio_bytes(data, mime or str(audio.get("mime_type") or ""))


def _archive_meta_media(message_db_id: int, meta_number_db_id: int, media_id: str, filename: str) -> None:
    db = SessionLocal()
    try:
        message = db.get(Message, message_db_id)
        number = db.get(MetaWhatsAppNumber, meta_number_db_id)
        if not message or not number or message.media_url:
            return
        try:
            data, mime = service_for_meta_number(number).download_media(media_id)
            storage_uri, _ = MediaStorage().upload_bytes(
                data=data,
                conversation_id=message.conversation_id,
                filename=filename or None,
                mime_type=mime or message.mime_type,
                direction="INBOUND",
            )
        except (MetaCloudError, MediaStorageError) as exc:
            LOG.warning("meta_media_archive_failed message=%s error=%s", message_db_id, type(exc).__name__)
            return
        message.media_url = storage_uri
        message.mime_type = mime or message.mime_type
        db.commit()
    finally:
        db.close()


@router.get("/webhooks/meta")
def meta_webhook_verify(request: Request, db: Session = Depends(get_db)):
    """Verificação do callback pela Meta (hub.challenge)."""
    params = request.query_params
    if params.get("hub.mode") != "subscribe":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    token = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge") or ""
    if not token or not challenge:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    for number in list_meta_numbers(db):
        if secrets.compare_digest(meta_verify_token(number), token):
            return PlainTextResponse(challenge[:200])
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhooks/meta")
async def meta_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    from app.routers.zapi import _process_zapi_payload

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return {"ok": True, "ignored": "object"}

    signature = request.headers.get("x-hub-signature-256")
    values = _changes(payload)
    numbers: dict[str, MetaWhatsAppNumber] = {}
    for value in values:
        phone_number_id = _phone_number_id(value)
        if phone_number_id and phone_number_id not in numbers:
            number = meta_number_by_phone_number_id(db, phone_number_id)
            if number:
                numbers[phone_number_id] = number
    if not numbers:
        # Evento de número não cadastrado aqui: nada a processar. Responder 200
        # evita a Meta reenviar e marcar o webhook como falho.
        LOG.info("meta_callback ignored=unknown_number changes=%s", len(values))
        return {"ok": True, "ignored": "unknown_number"}
    # O corpo só é confiável depois da assinatura: ela é feita com o App Secret
    # do app da Meta e só aceitamos o segredo de um número que está no próprio corpo.
    trusted_secret = ""
    for number in numbers.values():
        secret = meta_app_secret(number)
        if _signature_valid(raw_body, signature, secret):
            trusted_secret = secret
            break
    if not trusted_secret:
        LOG.warning("meta_callback rejected=signature numbers=%s", len(numbers))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    processed = 0
    for value in values:
        number = numbers.get(_phone_number_id(value))
        if not number or not hmac.compare_digest(meta_app_secret(number), trusted_secret):
            continue
        default_queue = default_queue_for_meta_number(db, number.id)
        queue_id = default_queue.id if default_queue else None

        for item in value.get("statuses") or []:
            if not isinstance(item, dict):
                continue
            internal = meta_status_to_internal(item)
            if internal:
                _process_zapi_payload(
                    internal,
                    db=db,
                    background_tasks=background_tasks,
                    queue_id=queue_id,
                    meta_number_id=number.id,
                    provider_label=PROVIDER_LABEL,
                )
                processed += 1

        names = _sender_names(value)
        for message in value.get("messages") or []:
            if not isinstance(message, dict):
                continue
            kind = str(message.get("type") or "")
            audio_text = _transcribe_meta_audio(number, message) if kind == "audio" else None
            internal = meta_message_to_internal(
                message,
                sender_name=names.get(str(message.get("from") or ""), ""),
                audio_text=audio_text,
            )
            if not internal:
                LOG.info("meta_callback ignored=unsupported number=%s type=%s", number.id, kind or "-")
                continue
            result = _process_zapi_payload(
                internal,
                db=db,
                background_tasks=background_tasks,
                queue_id=queue_id,
                meta_number_id=number.id,
                provider_label=PROVIDER_LABEL,
            )
            processed += 1
            media = message.get(kind) if isinstance(message.get(kind), dict) else {}
            if (
                kind in MEDIA_TYPES
                and result.get("created")
                and result.get("message_id")
                and media.get("id")
            ):
                background_tasks.add_task(
                    _archive_meta_media,
                    int(result["message_id"]),
                    number.id,
                    str(media["id"]),
                    str(media.get("filename") or ""),
                )
    LOG.info("meta_callback processed=%s numbers=%s", processed, len(numbers))
    return {"ok": True, "processed": processed}


# ------------------------------------------------------------- configuração


def _back(target: str = "/settings/whatsapp") -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def _subscribe_webhook(number: MetaWhatsAppNumber) -> str:
    """Inscreve o webhook; devolve mensagem de erro ou ""."""
    url = meta_webhook_url()
    if not url:
        return "Defina APP_PUBLIC_URL no Railway para registrar o webhook da Meta."
    try:
        service_for_meta_number(number).configure_webhooks(url)
    except MetaCloudError as exc:
        return str(exc)
    return ""


@router.post("/settings/meta/numbers")
def create_meta_number_route(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    phone_number_id: str = Form(...),
    waba_id: str = Form(...),
    access_token: str = Form(...),
    app_secret: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.timeutils import utcnow

    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    clean_id = "".join(ch for ch in phone_number_id if ch.isdigit())
    clean_waba = "".join(ch for ch in waba_id if ch.isdigit())
    if not clean_id or not clean_waba or not access_token.strip() or not app_secret.strip():
        flash(request, "Preencha Phone Number ID, WABA ID, token e App Secret.", "warning")
        return _back()
    if db.scalar(select(MetaWhatsAppNumber.id).where(MetaWhatsAppNumber.phone_number_id == clean_id)):
        flash(request, "Este número da Meta já está cadastrado.", "warning")
        return _back()

    item = create_meta_number(
        db,
        name=name or "WhatsApp oficial",
        phone_number_id=clean_id,
        waba_id=clean_waba,
        access_token=access_token,
        app_secret=app_secret,
        created_by_id=user.id,
    )
    try:
        refresh_meta_number_info(item)
    except MetaCloudError as exc:
        db.rollback()
        flash(request, f"Não foi possível validar o número na Meta: {exc}", "error")
        return _back()
    write_audit(
        db,
        user=user,
        action="META_NUMBER_CREATED",
        entity_type="meta_whatsapp_number",
        entity_id=item.id,
        payload={
            "name": item.name,
            "phone_number_id": item.phone_number_id,
            "waba_id": item.waba_id,
            "phone_label": item.phone_label,
            "verified_name": item.verified_name,
        },
    )
    # Salva antes de inscrever: a Meta chama o GET de verificação na hora.
    db.commit()

    problems: list[str] = []
    webhook_error = _subscribe_webhook(item)
    if webhook_error:
        problems.append(f"webhook: {webhook_error}")
    else:
        item.webhook_subscribed_at = utcnow()
    try:
        total = sync_meta_templates(db, item)
    except MetaCloudError as exc:
        problems.append(f"templates: {exc}")
        total = 0
    db.commit()
    if problems:
        flash(
            request,
            f"Número {item.name} cadastrado, com pendências — " + " | ".join(problems),
            "warning",
        )
    else:
        flash(request, f"Número {item.name} cadastrado: webhook registrado e {total} template(s) sincronizado(s).")
    return _back()


@router.post("/settings/meta/numbers/{number_id}/webhook")
def meta_number_webhook_route(
    number_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.timeutils import utcnow

    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    number = get_meta_number(db, number_id)
    if not number or not number.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    error = _subscribe_webhook(number)
    if error:
        flash(request, error, "error")
    else:
        number.webhook_subscribed_at = utcnow()
        db.commit()
        flash(request, f"Webhook da Meta registrado para {number.name}.")
    return _back()


@router.post("/settings/meta/numbers/{number_id}/templates/sync")
def meta_number_sync_templates_route(
    number_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    number = get_meta_number(db, number_id)
    if not number or not number.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        total = sync_meta_templates(db, number)
    except MetaCloudError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back()
    db.commit()
    flash(request, f"{total} template(s) sincronizado(s) da Meta.")
    return _back()


@router.post("/settings/meta/test")
def meta_test_message(
    request: Request,
    csrf_token: str = Form(...),
    meta_number_id: int = Form(...),
    phone: str = Form(...),
    message: str = Form(""),
    template_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    number = get_meta_number(db, meta_number_id)
    if not number or not number.active:
        flash(request, "Número da Meta inexistente ou inativo. Nada foi enviado.", "error")
        return _back()
    service = service_for_meta_number(number)
    try:
        if template_id:
            template = db.get(MetaMessageTemplate, template_id)
            if not template or template.meta_number_id != number.id or template.status != "APPROVED":
                flash(request, "Escolha um template aprovado deste número.", "warning")
                return _back()
            if template_variables(template.body_text) or template.header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
                flash(request, "Para o teste, use um template sem variáveis e sem mídia (ex.: hello_world).", "warning")
                return _back()
            result = service.send_template(phone=phone, name=template.name, language=template.language)
        else:
            result = service.send_text(phone=phone, message=message)
    except MetaCloudError as exc:
        flash(request, str(exc), "error")
        return _back()
    write_audit(
        db,
        user=user,
        action="META_TEST_SENT",
        entity_type="integration",
        entity_id="meta",
        payload={"provider_message_id": result.get("messageId"), "meta_number_id": number.id},
    )
    db.commit()
    flash(request, f"Teste enviado pela API oficial · {number.name} {number.phone_label}.")
    return _back()


@router.post("/settings/queues/{queue_id}/channel")
def set_queue_channel(
    queue_id: int,
    request: Request,
    csrf_token: str = Form(...),
    channel: str = Form(...),
    db: Session = Depends(get_db),
):
    """Escolhe o canal de envio da fila: Z-API (padrão) ou um número da Meta."""
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    queue = get_queue(db, queue_id)
    if not queue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    target = f"/settings/queues/{queue.id}"
    value = channel.strip().upper()
    if value == "ZAPI":
        changed = unbind_queue_meta_number(db, queue.id)
        payload = {"channel": "ZAPI", "changed": changed}
        message = f"Fila {queue.name} enviando pela Z-API."
    elif value.startswith("META:") and value[5:].isdigit():
        number = get_meta_number(db, int(value[5:]))
        if not number or not number.active:
            flash(request, "Selecione um número da Meta ativo.", "warning")
            return _back(target)
        bind_queue_meta_number(db, queue.id, number.id)
        payload = {"channel": "META", "meta_number_id": number.id}
        message = f"Fila {queue.name} enviando pela API oficial da Meta ({number.name})."
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    write_audit(
        db,
        user=user,
        action="SERVICE_QUEUE_CHANNEL_UPDATED",
        entity_type="service_queue",
        entity_id=queue.id,
        payload=payload,
    )
    db.commit()
    flash(request, message)
    return _back(target)


@router.post("/campaigns/{campaign_id}/meta-template")
def set_campaign_template(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    template_id: str = Form(""),
    params: str = Form(""),
    db: Session = Depends(get_db),
):
    """Template aprovado usado quando a campanha sai por uma fila da Meta."""
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not campaign_message_template(campaign):
        flash(request, "A campanha precisa ter uma mensagem de abordagem.", "warning")
        return _back("/campaigns")

    if not template_id.strip():
        set_campaign_meta_template(campaign, template_id=None, params=[])
        write_audit(
            db,
            user=user,
            action="CAMPAIGN_META_TEMPLATE_CLEARED",
            entity_type="campaign",
            entity_id=campaign.id,
            payload={},
        )
        db.commit()
        flash(request, f"Template da Meta removido da campanha {campaign.name}.")
        return _back("/campaigns")

    if not template_id.strip().isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    template = db.get(MetaMessageTemplate, int(template_id))
    if not template or template.status != "APPROVED":
        flash(request, "Escolha um template aprovado pela Meta.", "warning")
        return _back("/campaigns")
    reason = template_unsupported_reason(template)
    if reason:
        flash(request, f"O template {template.name} usa {reason}, que o disparo ainda não suporta.", "warning")
        return _back("/campaigns")
    values = [line.strip() for line in params.splitlines() if line.strip()]
    expected = len(template_variables(template.body_text))
    if len(values) != expected:
        flash(
            request,
            f"O template {template.name} tem {expected} variável(is); informe {expected} valor(es), um por linha.",
            "warning",
        )
        return _back("/campaigns")
    set_campaign_meta_template(campaign, template_id=template.id, params=values)
    write_audit(
        db,
        user=user,
        action="CAMPAIGN_META_TEMPLATE_SET",
        entity_type="campaign",
        entity_id=campaign.id,
        payload={"template": template.name, "language": template.language, "params": values},
    )
    db.commit()
    flash(request, f"Campanha {campaign.name} usará o template {template.name} nas filas da Meta.")
    return _back("/campaigns")
