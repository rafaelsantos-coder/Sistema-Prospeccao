from datetime import datetime

from app.services.followups import followup_dates, initial_next_action, next_followup_after


def test_followup_sequence_days():
    base = datetime(2026, 10, 27, 12, 0)
    dates = followup_dates(base)
    assert [(item - base).days for item in dates] == [3, 10, 20]


def test_next_followup_finishes_after_third():
    base = datetime(2026, 10, 27, 12, 0)
    assert (next_followup_after(base, 0) - base).days == 3
    assert (next_followup_after(base, 1) - base).days == 10
    assert (next_followup_after(base, 2) - base).days == 20
    assert next_followup_after(base, 3) is None


def test_external_team_gets_fast_initial_action():
    base = datetime(2026, 10, 27, 14, 0)
    result = initial_next_action("B2B", base)
    assert result > base
    assert (result - base).total_seconds() <= 24 * 3600
