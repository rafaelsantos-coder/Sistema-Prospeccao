"""Painel do motor WhatsApp Sulnet e entrada autenticada de eventos."""
from __future__ import annotations

import logging
import os
import secrets
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import (
    Message, SulnetWAInstance, SulnetWAReceipt,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.conversation_engine import ensure_contact, ensure_open_conversation, register_message
from app.services.queue_routing import queue_for_conversation
from app.services.sulnet_wa import (
    GatewayClient, GatewayConfig, SulnetWAError, list_instances, queue_for_instance, verify_event,
)
from app.timeutils import utcnow
from app.web import flash, render

LOG = logging.getLogger("sulnet_wa")
router = APIRouter()


def _commercial_activation_allowed() -> bool:
    """A ativação comercial exige autorização explícita, além da conexão do gateway."""
    return os.getenv("SULNET_WA_COMMERCIAL_ENABLED", "false").strip().lower() in {"true", "1", "yes", "on"}


def _admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    require_roles(user, "ADMIN", "GESTOR")
    return user, None


def _page(request: Request, db: Session, user, *, qr=None, qr_instance_id=None):
    config = GatewayConfig.from_env()
    instances = list_instances(db)
    status_by_id = {}
    gateway_reachable = False
    if config.configured:
        try:
            gateway_sessions = {x["id"]: x for x in GatewayClient(config).list_sessions()}
            gateway_reachable = True
        except SulnetWAError:
            gateway_sessions = {}
        for inst in instances:
            status_by_id[inst.id] = gateway_sessions.get(inst.session_id, {
                "status": "indisponível", "phone": "", "engineLoaded": False,
            })
    return render(
        request, "settings_sulnet_wa.html",
        user=user, active_nav="settings", settings_section="sulnet_wa",
        page_title="Motor WhatsApp Sulnet",
        gateway_configured=config.configured,
        gateway_reachable=gateway_reachable,
        lab_only=not _commercial_activation_allowed(),
        instances=instances, status_by_id=status_by_id,
        linked_queues={x.id: queue_for_instance(db, x.id) for x in instances},
        qr_data=qr, qr_instance_id=qr_instance_id,
    )


@router.get("/settings/sulnet-wa")
def admin_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin(request, db)
    if redirect:
        return redirect
    return _page(request, db, user)


@router.post("/settings/sulnet-wa/create")
def create_session(
    request: Request, csrf_token: str = Form(...), name: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _admin(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    label = name.strip()[:120]
    if not label or len(label) < 3:
        flash(request, "Informe um nome válido para a instância.", "warning")
        return RedirectResponse("/settings/sulnet-wa", status_code=303)
    session_id = "sulnet-" + secrets.token_hex(12)
    try:
        GatewayClient().create(session_id)
    except SulnetWAError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/settings/sulnet-wa", status_code=303)
    item = SulnetWAInstance(name=label, session_id=session_id, active=False)
    db.add(item)
    db.flush()
    write_audit(db, user=user, action="SULNET_WA_INSTANCE_CREATED",
                entity_type="sulnet_wa_instance", entity_id=item.id,
                payload={"session_id": session_id, "active": False})
    db.commit()
    flash(request, "Instância criada desativada. Inicie e pareie com número de teste.")
    return RedirectResponse("/settings/sulnet-wa", status_code=303)


@router.post("/settings/sulnet-wa/{instance_id}/{action}")
def session_action(
    instance_id: int, action: str, request: Request, csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _admin(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    instance = db.get(SulnetWAInstance, instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    if action not in {"start", "stop", "activate", "deactivate"}:
        raise HTTPException(status_code=404)
    try:
        if action == "start":
            GatewayClient().start(instance.session_id)
        elif action == "stop":
            GatewayClient().stop(instance.session_id)
        elif action == "activate":
            if not _commercial_activation_allowed():
                raise SulnetWAError("Ativação comercial bloqueada durante a homologação. Use somente um número de teste.")
            current = GatewayClient().status(instance.session_id)
            if current["status"] != "ready" or not current["phone"]:
                raise SulnetWAError("Pareamento não confirmado. Ativação bloqueada.")
            instance.phone_label = current["phone"]
            instance.active = True
        else:
            instance.active = False
    except SulnetWAError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/settings/sulnet-wa", status_code=303)
    write_audit(db, user=user, action="SULNET_WA_" + action.upper(),
                entity_type="sulnet_wa_instance", entity_id=instance.id,
                payload={"session_id": instance.session_id})
    db.commit()
    flash(request, "Ação aplicada à instância Sulnet WhatsApp.")
    return RedirectResponse("/settings/sulnet-wa", status_code=303)


@router.get("/settings/sulnet-wa/{instance_id}/qr")
def session_qr(instance_id: int, request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin(request, db)
    if redirect:
        return redirect
    instance = db.get(SulnetWAInstance, instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    try:
        qr = GatewayClient().qr(instance.session_id)
    except SulnetWAError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/settings/sulnet-wa", status_code=303)
    return _page(request, db, user, qr=qr, qr_instance_id=instance.id)


@router.post("/webhooks/sulnet-wa")
async def inbound_webhook(request: Request, db: Session = Depends(get_db)):
    # Without a valid cryptographic signature, refuse before loading any data.
    length = request.headers.get("content-length", "")
    if not length.isdecimal() or int(length) > 8192:
        raise HTTPException(status_code=413)
    body = await request.body()
    try:
        data = verify_event(body, request.headers.get("x-sulnet-wa-timestamp", ""),
                            request.headers.get("x-sulnet-wa-signature", ""))
    except SulnetWAError:
        raise HTTPException(status_code=403, detail="Evento não autorizado.") from None

    instance = db.scalar(select(SulnetWAInstance).where(
        SulnetWAInstance.session_id == data["sessionId"],
        SulnetWAInstance.active.is_(True),
    ))
    if not instance:
        raise HTTPException(status_code=409, detail="Instância não autorizada.")
    queue = queue_for_instance(db, instance.id)
    if queue is None:
        raise HTTPException(status_code=409, detail="Instância não vinculada a uma fila.")
    if db.get(SulnetWAReceipt, data["eventId"]) is not None:
        return {"ok": True, "duplicate": True}

    # A origem e explicitamente SULNET_WA, e somente a instância autorizada é vinculada.
    provider_id = "swa:" + instance.session_id + ":" + data["messageId"]
    if db.scalar(select(Message.id).where(Message.provider_message_id == provider_id)):
        return {"ok": True, "duplicate": True}
    contact = ensure_contact(db, data["from"])
    conversation = ensure_open_conversation(
        db, contact, source="SULNET_WA", queue_id=queue.id,
        queue_origin="SULNET_WA_INSTANCE", sulnet_wa_instance_id=instance.id,
        allow_unbound_reuse=False,
    )
    kind = data.get("kind")
    has_text = kind in {"conversation", "extendedTextMessage"}
    message = register_message(
        db, conversation, direction="INBOUND",
        message_type="TEXT" if has_text else "DOCUMENT",
        body=data["text"], provider_message_id=provider_id,
        status="RECEIVED", increment_unread=True,
    )
    db.add(SulnetWAReceipt(event_id=data["eventId"], instance_id=instance.id))
    db.flush()
    if has_text:
        from app.services.ai_sdr import enqueue_ai_job
        enqueue_ai_job(db, conversation=conversation, message=message)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(SulnetWAReceipt, data["eventId"]) is not None:
            return {"ok": True, "duplicate": True}
        raise
    LOG.info("sulnet_wa_webhook_processed instance=%s queue=%s conversation=%s",
             instance.id, queue.id, conversation.id)
    return {"ok": True, "duplicate": False}
