from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, WhatsAppPresence
from app.timeutils import utcnow


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def presence_cutoff() -> datetime:
    timeout = max(20, int(settings.whatsapp_presence_timeout_seconds or 45))
    return utcnow() - timedelta(seconds=timeout)


def _get_row(db: Session, user_id: int) -> WhatsAppPresence | None:
    return db.scalar(
        select(WhatsAppPresence).where(WhatsAppPresence.user_id == user_id)
    )


def is_presence_online(row: WhatsAppPresence | None) -> bool:
    if not row or row.status != "ONLINE" or not row.last_heartbeat_at:
        return False
    heartbeat = _naive_utc(row.last_heartbeat_at)
    return bool(heartbeat and heartbeat >= presence_cutoff())


def is_user_whatsapp_online(db: Session, user_id: int) -> bool:
    return is_presence_online(_get_row(db, user_id))


def join_whatsapp(db: Session, user: User) -> WhatsAppPresence:
    now = utcnow()
    row = _get_row(db, user.id)
    if not row:
        row = WhatsAppPresence(
            user_id=user.id,
            status="ONLINE",
            joined_at=now,
            last_heartbeat_at=now,
        )
        db.add(row)
        db.flush()
        return row

    if not is_presence_online(row):
        row.joined_at = now
    row.status = "ONLINE"
    row.last_heartbeat_at = now
    row.last_left_at = None
    db.flush()
    return row


def heartbeat_whatsapp(db: Session, user: User) -> WhatsAppPresence | None:
    row = _get_row(db, user.id)
    if not row or row.status != "ONLINE":
        return row
    row.last_heartbeat_at = utcnow()
    db.flush()
    return row


def leave_whatsapp(db: Session, user: User) -> WhatsAppPresence | None:
    row = _get_row(db, user.id)
    if not row:
        return None
    row.status = "OFFLINE"
    row.last_left_at = utcnow()
    db.flush()
    return row


def mark_whatsapp_assignment(db: Session, user_id: int) -> None:
    row = _get_row(db, user_id)
    if row:
        row.last_assigned_at = utcnow()
        db.flush()


def online_user_ids(db: Session) -> list[int]:
    cutoff = presence_cutoff()
    return list(
        db.scalars(
            select(WhatsAppPresence.user_id).where(
                WhatsAppPresence.status == "ONLINE",
                WhatsAppPresence.last_heartbeat_at.is_not(None),
                WhatsAppPresence.last_heartbeat_at >= cutoff,
            )
        )
    )


def presence_for_users(
    db: Session,
    user_ids: list[int],
) -> dict[int, WhatsAppPresence]:
    if not user_ids:
        return {}
    rows = list(
        db.scalars(
            select(WhatsAppPresence).where(
                WhatsAppPresence.user_id.in_(user_ids)
            )
        )
    )
    return {row.user_id: row for row in rows}


def current_presence_state(db: Session, user: User) -> dict[str, object]:
    row = _get_row(db, user.id)
    online = is_presence_online(row)
    return {
        "online": online,
        "status": "ONLINE" if online else "OFFLINE",
        "team": user.team,
        "branch": user.branch.city if user.branch else "Corporativo",
        "joined_at": row.joined_at.isoformat() if row and row.joined_at else None,
        "last_heartbeat_at": (
            row.last_heartbeat_at.isoformat()
            if row and row.last_heartbeat_at
            else None
        ),
        "timeout_seconds": max(
            20,
            int(settings.whatsapp_presence_timeout_seconds or 45),
        ),
    }
