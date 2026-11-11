"""Webhook do bot do Telegram e utilitários de convite."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import Lead
from app.security import get_current_user, validate_csrf
from app.services import telegram as tg
from app.services.audit import write_audit
from app.services.zapi import ZApiError
from app.web import flash

router = APIRouter()


@router.post("/webhooks/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    expected = settings.telegram_webhook_secret
    if not expected or not secrets.compare_digest(secret, expected):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if header and not secrets.compare_digest(header, expected):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc
    if not isinstance(update, dict):
        return JSONResponse({"ok": True})
    try:
        result = tg.process_update(db, update)
    except Exception:  # noqa: BLE001 - o Telegram reenvia em loop se devolvermos erro
        db.rollback()
        tg.LOG.exception("telegram_update_failed")
        result = {"ok": True, "error": "internal"}
    return JSONResponse(result)


@router.post("/settings/telegram/configure-webhook")
def configure_telegram_webhook(
    request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR"}:
        flash(request, "Somente gestores configuram o Telegram.", "warning")
        return RedirectResponse("/dashboard", status_code=303)
    validate_csrf(request, csrf_token)
    try:
        tg.set_webhook()
    except ZApiError as exc:
        flash(request, str(exc), "error")
    else:
        write_audit(db, user=user, action="TELEGRAM_WEBHOOK_CONFIGURED", entity_type="integration", entity_id="telegram", payload={})
        db.commit()
        flash(request, "Webhook do Telegram configurado.", "success")
    return RedirectResponse("/dispatches", status_code=303)


@router.post("/leads/{lead_id}/telegram-invite")
def telegram_invite(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    validate_csrf(request, csrf_token)
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404)
    url = tg.invite_url_for_lead(db, lead)
    if not url:
        return JSONResponse({"error": "indisponivel"}, status_code=409)
    db.commit()
    return JSONResponse({"url": url})
