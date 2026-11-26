"""Instagram-only inbox. No provider writes, CRM, or WhatsApp dispatch."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models_instagram import (
    instagram_inbox_members as members,
    instagram_inbox_messages as messages,
    instagram_inbox_threads as threads,
)
from app.services.secret_store import decrypt_secret, encrypt_secret
from app.timeutils import utc_naive, utcnow


def enabled() -> bool:
    return os.environ.get("INSTAGRAM_INBOX_ENABLED", "").strip().lower() in {"1", "true"}


def insert_for(db: Session):
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert


def _hash(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _id(value):
    return value if isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < len(value) <= 80 else None


def _party(payload, key):
    obj = payload.get(key)
    return _id(obj.get("id")) if isinstance(obj, dict) else None


def record_inbound(db: Session, account: str, kind: str, event: dict) -> bool:
    """Strict inbound message validation. Caller owns transaction. No backfill."""
    if kind == "changes" and event.get("field") == "messages":
        event = event.get("value")
    elif kind != "messaging":
        return False
    if not isinstance(event, dict):
        return False
    sender = _party(event, "sender")
    if not sender or sender == account or _party(event, "recipient") != account:
        return False
    message = event.get("message")
    if not isinstance(message, dict) or any(message.get(k) or event.get(k) for k in
        ("is_echo", "is_self", "is_self_reply", "is_deleted", "is_unsupported")):
        return False
    mid = message.get("mid")
    if not isinstance(mid, str) or not mid.strip() or len(mid) > 512 or "\0" in mid:
        return False
    raw = event.get("timestamp")
    if type(raw) is not int:
        return False
    try:
        stamp = datetime.fromtimestamp(raw / 1000, timezone.utc).replace(tzinfo=None)
    except (OverflowError, ValueError, OSError):
        return False
    now = utc_naive(utcnow())
    # Refuse ancient/sample timestamps and implausibly future events.
    if not now - timedelta(days=7) <= stamp <= now + timedelta(minutes=5):
        return False
    body = message.get("text")
    if not isinstance(body, str) or not body.strip():
        if not message.get("attachments"):
            return False
        body = "[Mídia recebida — visualização ainda não disponível]"
    body = body[:16000]
    thread_id = _hash(account, sender)
    message_key = _hash(account, mid)
    insert = insert_for(db)
    db.execute(insert(threads).values(
        thread_id=thread_id, account_id=account, last_inbound_at=stamp
    ).on_conflict_do_nothing(index_elements=["thread_id"]))
    result = db.execute(insert(messages).values(
        message_key=message_key, thread_id=thread_id,
        body_encrypted=encrypt_secret(body), received_at=stamp,
    ).on_conflict_do_nothing(index_elements=["message_key"]))
    if not result.rowcount:
        return False
    db.execute(update(threads).where(
        threads.c.thread_id == thread_id, threads.c.account_id == account,
        threads.c.last_inbound_at < stamp,
    ).values(last_inbound_at=stamp))
    return True


def is_member(db: Session, account: str, user_id: int) -> bool:
    return db.execute(select(members.c.user_id).where(
        members.c.account_id == account, members.c.user_id == user_id
    )).scalar_one_or_none() is not None


def list_threads(db: Session, account: str, user_id: int, manager: bool = False, limit: int = 100):
    query = select(threads).where(threads.c.account_id == account)
    if not manager:
        query = query.where(or_(threads.c.assigned_user_id.is_(None), threads.c.assigned_user_id == user_id))
    return db.execute(query.order_by(threads.c.last_inbound_at.desc(), threads.c.thread_id).limit(limit)).mappings().all()


def claim(db: Session, account: str, thread_id: str, user_id: int) -> bool:
    # Membership is checked inside the UPDATE too, not only in the HTTP guard.
    membership = select(members.c.user_id).where(
        members.c.account_id == account, members.c.user_id == user_id
    ).exists()
    result = db.execute(update(threads).where(
        threads.c.account_id == account, threads.c.thread_id == thread_id,
        threads.c.assigned_user_id.is_(None), membership,
    ).values(assigned_user_id=user_id, claimed_at=utcnow()))
    return result.rowcount == 1


def read_thread(db: Session, account: str, thread_id: str, user_id: int, manager: bool = False):
    query = select(threads).where(
        threads.c.account_id == account, threads.c.thread_id == thread_id
    )
    if not manager:
        query = query.where(threads.c.assigned_user_id == user_id)
    thread = db.execute(query).mappings().one_or_none()
    if thread is None:
        return None
    rows = db.execute(select(messages).where(
        messages.c.thread_id == thread_id
    ).order_by(messages.c.received_at.desc(), messages.c.message_key.desc()).limit(100)).mappings().all()
    return thread, [(row["received_at"], decrypt_secret(row["body_encrypted"])) for row in reversed(rows)]
