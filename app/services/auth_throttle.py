from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuthThrottle
from app.timeutils import utcnow


def throttle_key(email: str, ip: str | None) -> str:
    normalized = f"{email.strip().casefold()}|{(ip or '').strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def retry_after_seconds(
    db: Session,
    *,
    email: str,
    ip: str | None,
) -> int:
    row = db.get(AuthThrottle, throttle_key(email, ip))
    if not row or not row.blocked_until:
        return 0
    now = utcnow()
    if row.blocked_until <= now:
        return 0
    return max(1, int((row.blocked_until - now).total_seconds()))


def record_failure(
    db: Session,
    *,
    email: str,
    ip: str | None,
) -> int:
    key = throttle_key(email, ip)
    row = db.get(AuthThrottle, key)
    now = utcnow()
    window = timedelta(seconds=max(60, settings.login_window_seconds))
    block = timedelta(seconds=max(60, settings.login_block_seconds))

    if not row:
        row = AuthThrottle(
            key_hash=key,
            attempts=0,
            window_started_at=now,
        )
        db.add(row)
        db.flush()

    if now - row.window_started_at > window:
        row.attempts = 0
        row.window_started_at = now
        row.blocked_until = None

    row.attempts = int(row.attempts or 0) + 1
    if row.attempts >= max(3, settings.login_max_attempts):
        row.blocked_until = now + block
    row.updated_at = now

    if row.blocked_until and row.blocked_until > now:
        return max(1, int((row.blocked_until - now).total_seconds()))
    return 0


def clear_failures(
    db: Session,
    *,
    email: str,
    ip: str | None,
) -> None:
    row = db.get(AuthThrottle, throttle_key(email, ip))
    if row:
        db.delete(row)
