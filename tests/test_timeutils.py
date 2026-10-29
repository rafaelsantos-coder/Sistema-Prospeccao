from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.timeutils import is_past, utc_naive, utcnow


def test_utc_naive_accepts_aware_and_naive_datetimes():
    naive = datetime(2026, 10, 29, 12, 0, 0)
    aware = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

    assert utc_naive(naive) == naive
    assert utc_naive(aware) == naive


def test_is_past_handles_mixed_timezone_datetimes():
    reference = datetime(2026, 10, 29, 12, 0, 0)
    aware_past = datetime(2026, 10, 29, 11, 0, 0, tzinfo=timezone.utc)
    aware_future = datetime(2026, 10, 29, 13, 0, 0, tzinfo=timezone.utc)

    assert is_past(aware_past, reference) is True
    assert is_past(aware_future, reference) is False


def test_is_past_uses_current_utc_when_reference_is_omitted():
    assert is_past(utcnow() - timedelta(minutes=1)) is True
    assert is_past(utcnow() + timedelta(minutes=1)) is False
