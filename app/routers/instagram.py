"""Fase 1: callback oficial do Instagram com autenticação e caixa persistente.

Não dispara mensagens, não aciona SulIA, não importa contatos no CRM.
O roteamento comercial será ativado somente após testes de coexistência.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse

from app.database import get_db
from app.models_instagram import (
    instagram_webhook_events as events,
    instagram_inbox_members as members,
    instagram_inbox_threads as threads,
)
from app.security import require_permission, require_roles, require_user
from app.services.secret_store import SecretStoreError, decrypt_secret, encrypt_secret
from app.services.instagram_prospecting import preview_comments
from app.services import instagram_inbox as inbox
from app.routers.instagram_inbox import manager as inbox_manager, router as inbox_router
from app.timeutils import utcnow
from app.web import render

LOG = logging.getLogger("uvicorn.error")

router = APIRouter()
router.include_router(inbox_router)
MAX_BODY_BYTES = 1024 * 1024
ENV_NAMES = (
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_APP_ID",
    "INSTAGRAM_APP_SECRET",
    "INSTAGRAM_CONFIG_ID",
    "INSTAGRAM_WEBHOOK_VERIFY_TOKEN",
)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _account_id() -> str:
    value = _env("INSTAGRAM_ACCOUNT_ID")
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 80:
        raise HTTPException(status_code=503, detail="Instagram não configurado.")
    return value


@router.get("/webhooks/instagram", include_in_schema=False)
def verify_instagram(request: Request):
    _account_id()
    expected = _env("INSTAGRAM_WEBHOOK_VERIFY_TOKEN")
    if not expected or not _env("INSTAGRAM_APP_SECRET"):
        raise HTTPException(status_code=503, detail="Instagram não configurado.")
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")
    if mode != "subscribe" or not _equal(token, expected) or not challenge or len(challenge) > 512:
        raise HTTPException(status_code=403, detail="Verificação recusada.")
    return PlainTextResponse(challenge, headers={"Cache-Control": "no-store"})


async def _signed_payload(request: Request) -> dict:
    secret = _env("INSTAGRAM_APP_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Instagram não configurado.")
    signature = request.headers.get("x-hub-signature-256", "")
    if len(signature) != 71 or not signature.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Assinatura inválida.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Evento muito grande.")
        raw.extend(chunk)
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not _equal(signature, expected):
        raise HTTPException(status_code=403, detail="Assinatura inválida.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Evento inválido.")
    return payload


def _extract_events(payload: dict, account_id: str):
    if payload.get("object") != "instagram" or not isinstance(payload.get("entry"), list):
        return
    for entry in payload["entry"]:
        if not isinstance(entry, dict) or str(entry.get("id", "")) != account_id:
            continue
        for kind in ("messaging", "changes", "standby"):
            items = entry.get(kind)
            if not isinstance(items, list):
                continue
            for event in items:
                if isinstance(event, dict):
                    yield kind, event


@router.post("/webhooks/instagram", include_in_schema=False)
def receive_instagram(
    payload: dict = Depends(_signed_payload),
    db: Session = Depends(get_db),
):
    account_id = _account_id()
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    stored = matched = comments = direct_candidates = inbox_added = 0
    inbox_enabled = inbox.enabled()
    entries = payload.get("entry")
    is_instagram = payload.get("object") == "instagram"
    total_entries = len(entries) if isinstance(entries, list) else 0
    matching_entries = sum(
        isinstance(entry, dict) and str(entry.get("id", "")) == account_id
        for entry in entries
    ) if is_instagram and isinstance(entries, list) else 0
    for kind, event in _extract_events(payload, account_id):
        matched += 1
        if kind == "changes" and event.get("field") in ("comments", "live_comments"):
            comments += 1
        if (kind == "messaging" and isinstance(event.get("message"), dict)) or (
            kind == "changes" and event.get("field") == "messages"
        ):
            direct_candidates += 1
        canonical = json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(
            (account_id + ":" + kind + ":" + canonical).encode("utf-8")
        ).hexdigest()
        stmt = insert(events).values(
            event_hash=event_hash,
            account_id=account_id,
            event_kind=kind,
            payload_encrypted=encrypt_secret(canonical),
            received_at=utcnow(),
        ).on_conflict_do_nothing(index_elements=["event_hash"])
        stored += max(0, db.execute(stmt).rowcount)
        if inbox_enabled and inbox.record_inbound(db, account_id, kind, event):
            inbox_added += 1
    db.commit()
    # Aggregate diagnostics only: never log identifiers, secrets or message content.
    LOG.info(
        "instagram_ingress instagram_object=%s entries=%d matching_entries=%d "
        "matched=%d stored=%d comments=%d direct_candidates=%d "
        "inbox_added=%d inbox_enabled=%s",
        is_instagram, total_entries, matching_entries, matched, stored,
        comments, direct_candidates, inbox_added, inbox_enabled,
    )
    # 200 só é devolvido depois da persistência; falhas de banco geram retry da Meta.
    return {"ok": True, "stored": stored}


def _check_meta_account(account_id: str) -> dict:
    token = _env("INSTAGRAM_ACCESS_TOKEN")
    secret = _env("INSTAGRAM_APP_SECRET")
    if not token or not secret:
        return {"ok": False, "reason": "credentials_missing"}
    proof = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    # Login unificado usa Graph API do Facebook; leitura mínima, sem conteúdo de clientes.
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.get(
                f"https://graph.facebook.com/v26.0/{account_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "id,username", "appsecret_proof": proof},
            )
        if response.status_code != 200:
            return {"ok": False, "http_status": response.status_code}
        data = response.json()
        if not isinstance(data, dict) or str(data.get("id", "")) != account_id:
            return {"ok": False, "reason": "account_mismatch"}
        return {"ok": True, "account_id": account_id, "username": str(data.get("username", ""))[:100]}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "reason": "meta_unavailable"}


def _require_instagram_admin(request: Request, db: Session):
    user = require_user(request, db)
    require_roles(user, "ADMIN", "GESTOR")
    require_permission(user, "settings.connections.view")
    return user


def diagnostic_status(db: Session, account_id: str, check: bool = False) -> dict:
    count, latest = db.execute(
        select(func.count(), func.max(events.c.received_at))
        .where(events.c.account_id == account_id)
    ).one()
    by_kind = dict(db.execute(
        select(events.c.event_kind, func.count())
        .where(events.c.account_id == account_id)
        .group_by(events.c.event_kind)
    ).all())
    total_threads, assigned = db.execute(
        select(func.count(), func.count(threads.c.assigned_user_id))
        .select_from(threads).where(threads.c.account_id == account_id)
    ).one()
    member_count = db.execute(
        select(func.count()).select_from(members)
        .where(members.c.account_id == account_id)
    ).scalar_one()
    result = {
        "channel": "INSTAGRAM",
        "credentials_present": {name: bool(_env(name)) for name in ENV_NAMES},
        "events_stored": count,
        "last_event_at": latest.isoformat() if latest else None,
        "events_by_kind": {kind: by_kind.get(kind, 0) for kind in ("messaging", "changes", "standby")},
        "inbox_enabled": inbox.enabled(),
        "inbox_threads": total_threads,
        "inbox_pending": total_threads - assigned,
        "inbox_assigned": assigned,
        "inbox_members": member_count,
        "commercial_routing_enabled": False,
    }
    if check:
        result["meta_account"] = _check_meta_account(account_id)
    return result


@router.get("/settings/instagram/status", include_in_schema=False)
def instagram_status(request: Request, check: bool = False, db: Session = Depends(get_db)):
    _require_instagram_admin(request, db)
    return diagnostic_status(db, _account_id(), check)


@router.get("/instagram", include_in_schema=False)
def instagram_panel(request: Request, check: bool = False, db: Session = Depends(get_db)):
    user = _require_instagram_admin(request, db)
    status = diagnostic_status(db, _account_id(), check)
    # Shared visual layout; the Instagram nav suppresses WhatsApp presence requests.
    response = render(
        request, "instagram/panel.html", user=user, active_nav="instagram",
        page_title="Instagram – Digital", status=status,
        can_manage_members=inbox_manager(user),
    )
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return response


@router.get("/settings/instagram/prospecting/preview", include_in_schema=False)
def instagram_prospecting_preview(
    request: Request,
    mode: Literal["commercial", "all"] = "commercial",
    media_id: str | None = Query(None, pattern=r"^[0-9]{1,80}$"),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Read-only administrative dry run. No messages, assignments or CRM writes."""
    user = require_user(request, db)
    require_roles(user, "ADMIN", "GESTOR")
    require_permission(user, "settings.connections.view")
    account_id = _account_id()
    encrypted = db.execute(
        select(events.c.payload_encrypted)
        .where(events.c.account_id == account_id, events.c.event_kind == "changes")
        .order_by(events.c.received_at.desc(), events.c.event_hash.desc())
        .limit(limit)
    ).scalars().all()
    payloads = []
    unreadable = 0
    for item in encrypted:
        try:
            payloads.append(json.loads(decrypt_secret(item)))
        except (SecretStoreError, ValueError):
            unreadable += 1
    result = preview_comments(payloads, account_id, mode, media_id)
    result["rows_scanned"] = len(encrypted)
    result["unreadable"] = unreadable
    return result
