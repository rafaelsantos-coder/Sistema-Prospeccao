from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Activity, Lead, Message
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.conversation_engine import (
    ensure_contact,
    ensure_open_conversation,
    register_message,
    store_received_payload,
    update_message_statuses,
)
from app.services.followups import next_followup_after
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.metrics import apply_user_scope
from app.services.scoring import normalize_phone
from app.services.zapi import ZApiError, ZApiService
from app.timeutils import utcnow
from app.web import flash


router = APIRouter()
OPT_OUT_TERMS = (
    "nao quero",
    "não quero",
    "pare",
    "parar",
    "sair",
    "remover",
    "descadastrar",
    "nao me chame",
    "não me chame",
)


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
    if not isinstance(payload, dict):
        return {"ok": True, "ignored": "payload"}
    if settings.zapi_instance_id and payload.get("instanceId") not in {
        None,
        settings.zapi_instance_id,
    }:
        return {"ok": True, "ignored": "instance"}
    if payload.get("isGroup") or payload.get("isNewsletter"):
        return {"ok": True, "ignored": "non_direct"}

    if payload.get("type") == "MessageStatusCallback" or payload.get("ids"):
        updated = update_message_statuses(db, payload)
        write_audit(
            db,
            user=None,
            action="ZAPI_MESSAGE_STATUS",
            entity_type="integration",
            entity_id="zapi",
            payload={"status": payload.get("status"), "updated": updated},
        )
        db.commit()
        return {"ok": True, "status_updated": updated}

    conversation, message, created = store_received_payload(db, payload)
    if not conversation or not message:
        db.commit()
        return {"ok": True, "ignored": "unsupported_or_unmatched"}

    opt_out = False
    if created and message.direction == "INBOUND":
        lower_message = (message.body or "").casefold()
        opt_out = any(term in lower_message for term in OPT_OUT_TERMS)
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
                    description=f"Mensagem recebida via Z-API: {message.body}",
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
            },
        )
    db.commit()
    if (
        created
        and message.media_url
        and message.message_type in {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}
    ):
        background_tasks.add_task(_archive_message_media, message.id)
    return {
        "ok": True,
        "conversation_id": conversation.id,
        "message_id": message.id,
        "created": created,
        "opt_out": opt_out,
    }


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
    if not settings.zapi_webhook_url:
        flash(request, "Defina APP_PUBLIC_URL e ZAPI_WEBHOOK_SECRET no Railway.", "warning")
        return RedirectResponse("/settings", status_code=303)
    try:
        ZApiService().configure_webhooks(settings.zapi_webhook_url)
    except ZApiError as exc:
        flash(request, str(exc), "error")
    else:
        write_audit(
            db,
            user=user,
            action="ZAPI_WEBHOOK_CONFIGURED",
            entity_type="integration",
            entity_id="zapi",
        )
        db.commit()
        flash(request, "Webhooks da Z-API configurados com sucesso.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/zapi/test")
def test_zapi_message(
    request: Request,
    csrf_token: str = Form(...),
    phone: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    try:
        result = ZApiService().send_text(phone=phone, message=message)
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)
    provider_id = _provider_message_id(result)
    write_audit(
        db,
        user=user,
        action="ZAPI_TEST_SENT",
        entity_type="integration",
        entity_id="zapi",
        payload={"provider_message_id": provider_id},
    )
    db.commit()
    flash(request, "Mensagem de teste enviada pela Z-API.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/leads/{lead_id}/whatsapp")
def send_lead_whatsapp(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    message: str = Form(...),
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
    clean_message = message.strip()
    try:
        result = ZApiService().send_text(phone=lead.phone, message=clean_message)
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)

    try:
        contact = ensure_contact(db, lead.phone, lead=lead)
    except ValueError:
        flash(request, "Telefone invalido para o WhatsApp.", "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    conversation = ensure_open_conversation(db, contact, lead=lead, source=lead.source)
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
        payload={"provider_message_id": provider_id, "message_id": stored.id},
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
    except ZApiError as exc:
        return {"configured": settings.integrations["zapi"], "connected": False, "error": str(exc)}
