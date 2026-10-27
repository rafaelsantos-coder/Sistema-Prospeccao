from __future__ import annotations

from datetime import datetime, time, timedelta

from app.timeutils import LOCAL_TZ, UTC, utcnow


FOLLOW_UP_DAYS = (3, 10, 20)


def initial_next_action(team: str, created_at: datetime | None = None) -> datetime:
    created_utc = created_at or utcnow()
    aware_utc = created_utc.replace(tzinfo=UTC) if created_utc.tzinfo is None else created_utc.astimezone(UTC)
    local = aware_utc.astimezone(LOCAL_TZ)
    if team in {"PAP", "B2B"}:
        if local.time() >= time(17, 0):
            target = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            target = local.replace(hour=min(local.hour + 1, 17), minute=0, second=0, microsecond=0)
    else:
        target = local + timedelta(hours=2)
    return target.astimezone(UTC).replace(tzinfo=None)


def followup_dates(from_date: datetime | None = None) -> list[datetime]:
    base = from_date or utcnow()
    return [base + timedelta(days=days) for days in FOLLOW_UP_DAYS]


def next_followup_after(completed_at: datetime, completed_followups: int) -> datetime | None:
    if completed_followups >= len(FOLLOW_UP_DAYS):
        return None
    return completed_at + timedelta(days=FOLLOW_UP_DAYS[completed_followups])
