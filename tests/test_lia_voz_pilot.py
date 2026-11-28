"""Offline policy tests; never invoke an external SIP account or API."""
import pytest

from app.services.lia_voz_pilot import PilotConfig, PilotDenied


def config(**changes):
    values = dict(enabled=True, exclusive=True, prefix="8003")
    values.update(changes)
    return PilotConfig(**values)


def test_any_valid_brazilian_destination_and_native_account():
    assert config().authorize("+55 55 99999-0000", "1804") == "5555999990000"
    assert config().configured
    assert config().authorize("55 99999-0001", "1804") == "5555999990001"
    assert config().authorize("(11) 99999-0000", "1804") == "5511999990000"
    assert config().authorize("(55) 3333-0000", "1804") == "555533330000"
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0000", "1805")


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"exclusive": False},
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
    assert shared.authorize("55 99999-0001", "1804") == "5555999990001"


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
    assert config().authorize("55 99999-0001", "1804") == "5555999990001"


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
    monkeypatch.delenv("LIA_VOZ_TEST_PHONE", raising=False)
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
            secondary = VoiceSipAccount(
                name="Secondary CI", domain="sip.example.invalid", username="1805",
                transport="UDP", password_encrypted="not-a-real-secret", active=True,
            )
            inactive = VoiceSipAccount(
                name="Inactive CI", domain="sip.example.invalid", username="1806",
                transport="UDP", password_encrypted="not-a-real-secret", active=False,
            )
            db.add_all([secondary, inactive])
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

            def request(mode="ia", selected=None, route="account",
                        phone="5555999990000", authorized="sim"):
                return voice.request_lia_voz_test(
                    request=SimpleNamespace(), csrf_token="test",
                    account_id=account_id if selected is None else selected,
                    phone=phone, authorized=authorized, mode=mode, route=route, db=db,
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
            assert "1804" in request("sip", secondary.id, "native")
            assert "indisponivel" in request("sip", inactive.id)
            # A different number is accepted for either SIP account and mode,
            # but authorization is always checked before queueing the call.
            assert "Confirme" in request("sip", secondary.id, phone="11999990001", authorized="")
            assert "invalida" in request("sip", secondary.id, phone="190")
            for mode, phone in (("sip", "11999990001"), ("ia", "21999990002")):
                assert request(mode, secondary.id, phone=phone).status_code == 303
                pending = db.scalar(select(VoiceTestCall).where(
                    VoiceTestCall.status.in_(ACTIVE),
                    VoiceTestCall.account_id == secondary.id,
                ))
                assert pending is not None and pending.account_label == secondary.name
                assert pending.message.startswith("CONTA |")
                assert pending.target_phone == "55" + phone
                assert "outro teste" in request("ia", account_id)
                pending.status = "LIA_FINALIZADA"
                pending.finished_at = now
                db.commit()
    finally:
        engine.dispose()


def test_secondary_account_normal_route_with_multiple_destinations():
    c = config()
    assert c.authorize("55 99999-0000", "1805", route="account") == "5555999990000"
    assert c.authorize("55 99999-0001", "1805", route="account") == "5555999990001"
    with pytest.raises(PilotDenied):
        c.authorize("55 99999-0000", "1805", route="native")
    with pytest.raises(PilotDenied):
        c.authorize("55 99999-0000", "", route="account")
    with pytest.raises(PilotDenied):
        config(exclusive=False).authorize("55 99999-0000", "1805", route="account")
    c = config(prefix="9999")
    assert c.configured and not c.native_configured
    assert c.authorize("55 99999-0000", "1805", route="account") == "5555999990000"
    with pytest.raises(PilotDenied):
        c.authorize("55 99999-0000", "1804", route="native")


@pytest.mark.parametrize("phone", ["", "190", "08001234567", "00999990000", "abc", "+1 2125550100"])
@pytest.mark.parametrize("route", ["account", "native"])
def test_invalid_or_non_brazilian_destinations_still_rejected(phone, route):
    with pytest.raises(PilotDenied):
        config().authorize(phone, "1804", route=route)


def test_no_fixed_destination_environment_configuration(monkeypatch):
    monkeypatch.setenv("LIA_VOZ_TEST_ENABLED", "true")
    monkeypatch.setenv("LIA_VOZ_TEST_EXCLUSIVE_SIP", "true")
    monkeypatch.setenv("LIA_VOZ_TEST_NATIVE_PREFIX", "8003")
    # A leftover variable from the old rollout must not affect admission.
    monkeypatch.setenv("LIA_VOZ_TEST_PHONE", "5555999990000")
    c = PilotConfig.from_env()
    assert c.configured and c.native_configured
    assert c.authorize("21999990002", "1805", route="account") == "5521999990002"
    monkeypatch.delenv("LIA_VOZ_TEST_PHONE")
    assert PilotConfig.from_env().configured
    assert PilotConfig.from_env().native_configured


def test_worker_revalidates_persisted_destination_and_blacklist(monkeypatch):
    from types import SimpleNamespace as NS
    from app.models import User, VoiceDialerEngine, VoiceSipAccount, VoiceTestCall
    from app.services.lia_voz_pilot import ACCOUNT_RUNNING, ACCOUNT_PROBE_RUNNING
    from app.workers import lia_voz_pilot as worker

    monkeypatch.setenv("LIA_VOZ_TEST_ENABLED", "true")
    monkeypatch.setenv("LIA_VOZ_TEST_EXCLUSIVE_SIP", "true")
    monkeypatch.delenv("LIA_VOZ_TEST_PHONE", raising=False)
    monkeypatch.setattr(worker, "runtime_ready", lambda: True)
    monkeypatch.setattr(worker, "slot_healthy", lambda **_: True)
    monkeypatch.setattr(worker, "ensure_idle", lambda *_args, **_kwargs: None)
    checked = []
    monkeypatch.setattr(worker, "outbound_guard", lambda _db, phone: checked.append(phone))
    number = "5511999990001"
    row = NS(status=ACCOUNT_RUNNING, initiator_user_id=1, account_id=2, target_phone=number)
    actor = NS(active=True, role="ADMIN")
    account = NS(active=True, username="1805")
    rows = {VoiceDialerEngine: NS(enabled=True), VoiceSipAccount: account,
            VoiceTestCall: row, User: actor}

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, model, _id):
            return rows[model]

    monkeypatch.setattr(worker, "SessionLocal", DB)
    for status in (ACCOUNT_RUNNING, ACCOUNT_PROBE_RUNNING):
        row.status = status
        assert worker._guard(7, number, 2)
        assert checked[-1] == number
        assert not worker._guard(7, "5511999990002", 2)
        assert not worker._guard(7, number, 3)
    actor.active = False
    assert not worker._guard(7, number, 2)
    actor.active = True
    account.active = False
    assert not worker._guard(7, number, 2)
    account.active = True

    def blocked(_db, _number):
        raise ValueError("blacklist")

    monkeypatch.setattr(worker, "outbound_guard", blocked)
    assert not worker._guard(7, number, 2)
