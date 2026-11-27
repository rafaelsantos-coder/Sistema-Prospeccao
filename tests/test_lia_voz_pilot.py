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


def test_manual_lia_test_has_no_hourly_quota_and_still_rejects_overlap(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models import VoiceSipAccount, VoiceTestCall, VoiceDialerEngine
    from app.routers import voice
    from app.services.lia_voz_pilot import (
        ensure_idle, ACCOUNT_PENDING, ACTIVE,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    actor = SimpleNamespace(id=1, role="ADMIN", active=True)
    monkeypatch.setenv("LIA_VOZ_TEST_ENABLED", "true")
    monkeypatch.setenv("LIA_VOZ_TEST_EXCLUSIVE_SIP", "true")
    monkeypatch.setenv("LIA_VOZ_TEST_PHONE", "5555999990000")
    monkeypatch.setenv("LIA_VOZ_TEST_NATIVE_PREFIX", "8003")
    monkeypatch.setenv("LIA_VOZ_TEST_SHARED_ENGINE", "true")
    monkeypatch.setattr(voice, "_actor", lambda *_args: (actor, None))
    monkeypatch.setattr(voice, "require_roles", lambda *_args: None)
    monkeypatch.setattr(voice, "validate_csrf", lambda *_args: None)
    monkeypatch.setattr(voice, "outbound_guard", lambda *_args: None)
    monkeypatch.setattr(voice, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(voice, "flash", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(voice, "_error", lambda _request, message, _destination: message)

    try:
        with Session(engine) as db:
            account = VoiceSipAccount(
                name="Native CI", domain="sip.example.invalid", username="1804",
                transport="UDP", password_encrypted="not-a-real-secret", active=True,
            )
            db.add(account)
            db.add(VoiceDialerEngine(
                id=1, enabled=True, heartbeat_at=now, lia_runtime_enabled=True,
                campaign_runtime_enabled=True, test_runtime_enabled=True,
            ))
            db.flush()
            account_id = account.id
            # More than three completed attempts in the last minute, including
            # one completed immediately before the next request.
            for _ in range(12):
                db.add(VoiceTestCall(
                    account_id=account_id, account_label=account.name,
                    target_phone="5555999990000", status="LIA_PROBE_FINALIZADO",
                    created_at=now - timedelta(seconds=15), finished_at=now,
                ))
            db.commit()

            def request(mode="ia"):
                return voice.request_lia_voz_test(
                    request=SimpleNamespace(), csrf_token="test",
                    account_id=account_id, phone="5555999990000",
                    authorized="sim", mode=mode, route="account", db=db,
                )

            for mode in ("ia", "sip", "ia"):
                response = request(mode)
                assert response.status_code == 303
                pending = db.scalar(select(VoiceTestCall).where(
                    VoiceTestCall.status.in_(ACTIVE),
                    VoiceTestCall.account_id == account_id,
                ))
                assert pending is not None
                if mode == "ia":
                    assert pending.status == ACCOUNT_PENDING
                assert "outro teste" in request()
                with pytest.raises(PilotDenied, match="outro teste"):
                    ensure_idle(db, shared=True, admission=True)
                pending.status = "LIA_FINALIZADA"
                pending.finished_at = now
                db.commit()
            assert len(db.scalars(select(VoiceTestCall).where(
                VoiceTestCall.account_id == account_id
            )).all()) == 15
    finally:
        engine.dispose()
