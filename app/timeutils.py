from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


UTC = timezone.utc
LOCAL_TZ = ZoneInfo(settings.default_timezone)


def utcnow() -> datetime:
    """UTC sem tzinfo para compatibilidade uniforme entre SQLite e PostgreSQL."""
    return datetime.now(UTC).replace(tzinfo=None)


def local_input_to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        local_value = datetime.fromisoformat(value)
    except ValueError:
        return None
    if local_value.tzinfo is None:
        local_value = local_value.replace(tzinfo=LOCAL_TZ)
    return local_value.astimezone(UTC).replace(tzinfo=None)


def utc_to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ)


def local_day_start_utc(reference: datetime | None = None) -> datetime:
    reference = reference or datetime.now(LOCAL_TZ)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=LOCAL_TZ)
    local_start = reference.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC).replace(tzinfo=None)
