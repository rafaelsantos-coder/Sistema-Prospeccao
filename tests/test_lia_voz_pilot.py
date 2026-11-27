"""Offline policy tests; never invoke an external SIP account or API."""
import pytest

from app.services.lia_voz_pilot import PilotConfig, PilotDenied


def config(**changes):
    values = dict(enabled=True, exclusive=True,
                  allowed_phone="(55) 99999-0000", prefix="8003")
    values.update(changes)
    return PilotConfig(**values)


def test_exact_canonical_destination_and_account():
    assert config().authorize("+55 55 99999-0000", "1804") == "5555999990000"
    assert config().configured
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0001", "1804")
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0000", "1805")


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"exclusive": False}, {"allowed_phone": ""},
    {"prefix": "9999"}, {"allowed_phone": "190"},
])
def test_missing_or_invalid_config_fails_closed(changes):
    c = config(**changes)
    assert not c.configured
    with pytest.raises(PilotDenied):
        c.authorize("55 99999-0000", "1804")


def test_environment_defaults_off(monkeypatch):
    for name in ("LIA_VOZ_TEST_ENABLED", "LIA_VOZ_TEST_EXCLUSIVE_SIP",
                 "LIA_VOZ_TEST_PHONE", "LIA_VOZ_TEST_NATIVE_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    assert not PilotConfig.from_env().configured


def test_shared_config_does_not_change_number_authorization():
    shared = config(shared=True)
    assert shared.request_seconds == 600
    assert shared.authorize("55 99999-0000", "1804") == "5555999990000"
    with pytest.raises(PilotDenied):
        shared.authorize("55 99999-0001", "1804")


def test_shared_admission_only_checks_duplicate_lia_request():
    from app.services.lia_voz_pilot import ensure_idle

    class DB:
        def __init__(self, duplicate=None):
            self.calls = 0
            self.duplicate = duplicate

        def scalar(self, statement):
            self.calls += 1
            assert "voice_test_calls" in str(statement)
            return self.duplicate

    db = DB()
    ensure_idle(db, shared=True, admission=True)
    assert db.calls == 1
    with pytest.raises(PilotDenied):
        ensure_idle(DB(1), shared=True, admission=True)


def test_shared_execution_checks_active_calls_not_running_campaign():
    from app.services.lia_voz_pilot import ensure_idle

    class DB:
        def __init__(self, active=False):
            self.active = active
            self.queries = []

        def scalar(self, statement):
            sql = str(statement)
            self.queries.append(sql)
            assert "voice_campaigns" not in sql
            if self.active and "voice_recipients" in sql:
                return 123
            return None

    db = DB()
    ensure_idle(db, shared=True, own_test_id=1)
    assert len(db.queries) == 3
    with pytest.raises(PilotDenied):
        ensure_idle(DB(active=True), shared=True, own_test_id=1)


def test_probe_is_explicit_and_shares_active_states():
    from app.services.lia_voz_pilot import (
        requested_state, PENDING, PROBE_PENDING, PROBE_RUNNING, ACTIVE,
        PENDING_STATES, RUNNING_STATES,
    )
    assert requested_state("ia") == PENDING
    assert requested_state("sip") == PROBE_PENDING
    assert PROBE_PENDING in PENDING_STATES and PROBE_RUNNING in RUNNING_STATES
    assert {PROBE_PENDING, PROBE_RUNNING} <= ACTIVE
    with pytest.raises(PilotDenied):
        requested_state("campaign")


def test_explicit_account_route_has_own_persisted_states_and_same_guard():
    from app.services.lia_voz_pilot import (
        requested_state, ACCOUNT_PENDING, ACCOUNT_RUNNING,
        ACCOUNT_PROBE_PENDING, ACCOUNT_PROBE_RUNNING,
        ACTIVE, ACCOUNT_STATES, PROBE_STATES, CLAIM_STATES,
    )
    assert requested_state("ia", "account") == ACCOUNT_PENDING
    assert requested_state("sip", "account") == ACCOUNT_PROBE_PENDING
    assert CLAIM_STATES[ACCOUNT_PENDING] == ACCOUNT_RUNNING
    assert CLAIM_STATES[ACCOUNT_PROBE_PENDING] == ACCOUNT_PROBE_RUNNING
    assert ACCOUNT_RUNNING in ACCOUNT_STATES
    assert ACCOUNT_PROBE_RUNNING in ACCOUNT_STATES & PROBE_STATES
    assert all(len(state) <= 30 for state in ACTIVE)
    for route in ("", "8004", "auto", "fallback"):
        with pytest.raises(PilotDenied):
            requested_state("sip", route)
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0001", "1804")


def test_manual_test_can_follow_immediately_but_hourly_cap_and_overlap_remain():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models import VoiceSipAccount, VoiceTestCall
    from app.services.lia_voz_pilot import (
        ensure_hourly_quota, ensure_idle, ACCOUNT_PENDING,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as db:
            account = VoiceSipAccount(
                name="Native CI", domain="sip.example.invalid", username="1804",
                transport="UDP", password_encrypted="not-a-real-secret", active=True,
            )
            db.add(account)
            db.flush()
            def add_call(status, created_at, finished_at):
                call = VoiceTestCall(
                    account_id=account.id, account_label=account.name,
                    target_phone="5555999990000", status=status,
                    created_at=created_at, finished_at=finished_at,
                )
                db.add(call)
                db.flush()
                return call

            add_call("SIP_VALIDADO", now, now)
            add_call("LIA_FALHOU", now - timedelta(hours=2), now - timedelta(hours=2))
            add_call("LIA_PROBE_FINALIZADO", now - timedelta(seconds=12), now)
            # Even a test that finished just now no longer causes a 3min delay.
            ensure_idle(db, shared=True, admission=True)
            ensure_hourly_quota(db, account.id, now)
            pending = add_call(ACCOUNT_PENDING, now, None)
            # Two recent attempts: quota allows another after completion, but
            # a duplicate submission while the current test is pending fails.
            ensure_hourly_quota(db, account.id, now)
            with pytest.raises(PilotDenied, match="outro teste"):
                ensure_idle(db, shared=True, admission=True)
            pending.status = "LIA_FINALIZADA"
            pending.finished_at = now
            db.flush()
            ensure_idle(db, shared=True, admission=True)
            ensure_hourly_quota(db, account.id, now)
            add_call("LIA_FINALIZADA", now, now)
            with pytest.raises(PilotDenied, match="tres testes por hora"):
                ensure_hourly_quota(db, account.id, now)
    finally:
        engine.dispose()
