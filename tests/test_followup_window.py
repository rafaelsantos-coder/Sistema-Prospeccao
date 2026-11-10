"""Follow-ups só no horário comercial e no máximo um sem resposta por dia."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import ai_followups
from app.timeutils import UTC

SP = ZoneInfo("America/Sao_Paulo")


def _utc(local: str) -> datetime:
    return datetime.fromisoformat(local).replace(tzinfo=SP).astimezone(UTC).replace(tzinfo=None)


def _local(value: datetime) -> str:
    return value.replace(tzinfo=UTC).astimezone(SP).strftime("%a %Y-%m-%d %H:%M")


def test_business_window_moves_night_and_weekend_to_next_opening():
    assert ai_followups.business_window_start(_utc("2026-11-03 10:00")) == _utc("2026-11-03 10:00")
    assert _local(ai_followups.business_window_start(_utc("2026-11-04 00:12"))) == "Wed 2026-11-04 09:00"
    assert _local(ai_followups.business_window_start(_utc("2026-11-03 23:40"))) == "Wed 2026-11-04 09:00"
    assert _local(ai_followups.business_window_start(_utc("2026-11-07 14:00"))) == "Mon 2026-11-09 09:00"
    assert _local(ai_followups.business_window_start(_utc("2026-11-08 11:00"))) == "Mon 2026-11-09 09:00"


class _Db:
    def __init__(self, *values):
        self.values = list(values)

    def scalar(self, _stmt):
        return self.values.pop(0)


def test_second_unanswered_followup_same_day_moves_to_next_day():
    last_inbound = _utc("2026-11-03 10:00")
    first_followup_sent = _utc("2026-11-03 10:10")
    due_same_day = _utc("2026-11-03 10:40")

    due = ai_followups._limit_one_unanswered_per_day(
        _Db(last_inbound, first_followup_sent), 1, due_same_day,
    )

    assert _local(due) == "Wed 2026-11-04 09:00"


def test_first_followup_after_customer_reply_keeps_cadence():
    due_same_day = _utc("2026-11-03 10:40")

    due = ai_followups._limit_one_unanswered_per_day(
        _Db(_utc("2026-11-03 10:30"), None), 1, due_same_day,
    )

    assert due == due_same_day
