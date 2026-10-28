from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.database import get_db
from app.models import User, WhatsAppPresence
from app.security import get_current_user, validate_csrf
from app.services.audit import write_audit
from app.services.whatsapp_presence import (
    current_presence_state,
    heartbeat_whatsapp,
    join_whatsapp,
    leave_whatsapp,
    presence_cutoff,
)


router = APIRouter()


def _current_user_or_401(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def _group_online_count(db: Session, user: User) -> int:
    stmt = (
        select(func.count(WhatsAppPresence.id))
        .join(User, WhatsAppPresence.user_id == User.id)
        .where(
            User.active.is_(True),
            User.team == user.team,
            WhatsAppPresence.status == "ONLINE",
            WhatsAppPresence.last_heartbeat_at.is_not(None),
            WhatsAppPresence.last_heartbeat_at >= presence_cutoff(),
        )
    )
    if user.branch_id:
        stmt = stmt.where(User.branch_id == user.branch_id)
    return int(db.scalar(stmt) or 0)


def _state(db: Session, user: User) -> dict[str, object]:
    payload = current_presence_state(db, user)
    payload["group_online_count"] = _group_online_count(db, user)
    return payload


@router.get("/api/whatsapp-presence")
def whatsapp_presence_state(
    request: Request,
    db: Session = Depends(get_db),
):
    user = _current_user_or_401(request, db)
    return _state(db, user)


@router.post("/api/whatsapp-presence/join")
def whatsapp_presence_join(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _current_user_or_401(request, db)
    validate_csrf(request, csrf_token)
    join_whatsapp(db, user)
    write_audit(
        db,
        user=user,
        action="WHATSAPP_PRESENCE_ONLINE",
        entity_type="whatsapp_presence",
        entity_id=user.id,
        payload={"team": user.team, "branch_id": user.branch_id},
    )
    db.commit()
    return JSONResponse({"ok": True, **_state(db, user)})


@router.post("/api/whatsapp-presence/heartbeat")
def whatsapp_presence_heartbeat(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _current_user_or_401(request, db)
    validate_csrf(request, csrf_token)
    row = heartbeat_whatsapp(db, user)
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "online": bool(row and row.status == "ONLINE"),
            **_state(db, user),
        }
    )


@router.post("/api/whatsapp-presence/leave")
def whatsapp_presence_leave(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _current_user_or_401(request, db)
    validate_csrf(request, csrf_token)
    leave_whatsapp(db, user)
    write_audit(
        db,
        user=user,
        action="WHATSAPP_PRESENCE_OFFLINE",
        entity_type="whatsapp_presence",
        entity_id=user.id,
        payload={"team": user.team, "branch_id": user.branch_id},
    )
    db.commit()
    return JSONResponse({"ok": True, **_state(db, user)})
