"""Lia Voz single test queue, consumed only by an explicitly isolated voice engine."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal, engine as database_engine
from app.models import User, VoiceDialerEngine, VoiceSipAccount, VoiceTestCall
from app.services.lia_voz_call import perform_lia_test, perform_native_probe
from app.services.lia_voz_realtime import LiaVoiceError, safe_realtime_failure
from app.services.lia_voz_pilot import (
    ACTIVE, PENDING_STATES, RUNNING_STATES, CLAIM_STATES, PROBE_STATES,
    ACCOUNT_STATES, PilotConfig, PilotDenied, ensure_idle,
)
from app.services.voice_execution_slot import slot_healthy
from app.services.secret_store import decrypt_secret
from app.services.voice_pjsua2 import PjSession
from app.services.voice_runtime import outbound_guard
from app.services.voice_test import aware


def _now():
    return datetime.now(timezone.utc)


def runtime_ready():
    config = PilotConfig.from_env()
    if not config.configured or not os.getenv("LIA_VOZ_OPENAI_API_KEY", "").strip():
        return False
    if config.shared:
        return database_engine.dialect.name == "postgresql"
    return (os.getenv("VOICE_CAMPAIGN_OUTBOUND_ENABLED", "").lower() != "true"
            and os.getenv("VOICE_TRANSFER_TESTS_ENABLED", "").lower() != "true")


def _finish(test_id, status, message, sip_code=None, register_code=None):
    with SessionLocal() as db:
        row = db.get(VoiceTestCall, test_id)
        if row is None or row.status not in RUNNING_STATES:
            return
        row.status = status
        row.message = message[:230]
        row.sip_invite_code = sip_code
        if register_code is not None:
            row.sip_register_code = register_code
        row.finished_at = _now()
        db.commit()


def expire_stale():
    with SessionLocal() as db:
        rows = db.scalars(select(VoiceTestCall).where(
            VoiceTestCall.status.in_(ACTIVE)
        ).with_for_update(skip_locked=True))
        for row in rows:
            base = row.started_at if row.status in RUNNING_STATES else row.created_at
            limit = 240 if row.status in RUNNING_STATES else PilotConfig.from_env().request_seconds
            if base and _now() - aware(base) > timedelta(seconds=limit):
                row.status = "LIA_INDETERMINADO" if row.status in RUNNING_STATES else "LIA_EXPIRADO"
                row.finished_at = _now()
                row.message = "Teste expirado; nao repetir automaticamente."
        db.commit()


def claim():
    if not runtime_ready() or not slot_healthy(required=True):
        return None
    with SessionLocal() as db:
        engine = db.scalar(select(VoiceDialerEngine).where(
            VoiceDialerEngine.id == 1
        ).with_for_update())
        if not engine or not engine.enabled:
            return None
        row = db.scalar(select(VoiceTestCall).where(
            VoiceTestCall.status.in_(PENDING_STATES)
        ).order_by(VoiceTestCall.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        config = PilotConfig.from_env()
        if _now() - aware(row.created_at) > timedelta(seconds=config.request_seconds):
            row.status = "LIA_EXPIRADO"
            row.finished_at = _now()
            row.message = "Solicitacao de teste expirou."
            db.commit()
            return None
        try:
            ensure_idle(db, own_test_id=row.id, shared=config.shared)
        except PilotDenied as exc:
            # Surface the reason to the administrator without leaking a number
            # or polling logs. The request stays pending until safe or expired.
            message = str(exc)[:200] + " Aguardando vaga."
            if row.message != message:
                row.message = message
                db.commit()
            return None
        row.status = CLAIM_STATES[row.status]
        route_label = "NATIONAL" if row.status in ACCOUNT_STATES else config.prefix
        row.message = (route_label + " | " +
                       ("Motor reservado; iniciando teste da rota sem IA."
                        if row.status in PROBE_STATES
                        else "Motor reservado; iniciando homologacao SIP/Realtime."))
        row.started_at = _now()
        db.commit()
        return row.id


def _guard(test_id, phone, account_id):
    if not runtime_ready() or not slot_healthy(required=True):
        return False
    try:
        with SessionLocal() as db:
            engine = db.get(VoiceDialerEngine, 1)
            account = db.get(VoiceSipAccount, account_id)
            test = db.get(VoiceTestCall, test_id)
            actor = db.get(User, test.initiator_user_id) if test and test.initiator_user_id else None
            if not (engine and engine.enabled and account and account.active
                    and test and test.status in RUNNING_STATES and actor and actor.active
                    and actor.role == "ADMIN" and test.account_id == account_id
                    and test.target_phone == phone):
                return False
            PilotConfig.from_env().authorize(phone, account.username)
            ensure_idle(db, own_test_id=test_id, shared=PilotConfig.from_env().shared)
            outbound_guard(db, phone)
        return True
    except Exception:
        return False


def _safe_failure_kind(exc):
    # Do not store raw SIP/WebSocket exceptions (which may contain secrets).
    if isinstance(exc, TimeoutError):
        return "tempo limite"
    if isinstance(exc, OSError):
        return "falha de rede/SO"
    if isinstance(exc, LiaVoiceError):
        return "falha Realtime/midia: " + safe_realtime_failure(exc)
    if type(exc).__name__ == "InvalidStatus":
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return "Realtime HTTP " + str(code) if isinstance(code, int) else "falha HTTP"
    return "falha interna"


def run(test_id, *, refresh_heartbeat=lambda: None):
    register_code = None
    last_code = None
    last_stage = "Validando autorizacao e conta SIP."
    route_label = ""

    def progress(message, sip_code=None):
        nonlocal last_stage, last_code
        last_stage = ((route_label + " | " if route_label else "") + message)[:230]
        if sip_code is not None:
            last_code = sip_code
        logging.getLogger(__name__).info("Lia Voz teste %d: %s", test_id, last_stage)
        # A progress-write failure must not trigger another INVITE. The existing
        # authorization guard still checks database/engine state before dialing.
        try:
            with SessionLocal() as db:
                row = db.get(VoiceTestCall, test_id)
                if row is not None and row.status in RUNNING_STATES:
                    row.message = last_stage
                    row.sip_register_code = register_code
                    row.sip_invite_code = last_code
                    db.commit()
        except Exception:
            logging.getLogger(__name__).warning("Lia Voz teste %d: progresso indisponivel", test_id)

    try:
        with SessionLocal() as db:
            test = db.get(VoiceTestCall, test_id)
            if not test or test.status not in RUNNING_STATES or not test.account_id:
                return
            probe = test.status in PROBE_STATES
            account_route = test.status in ACCOUNT_STATES
            account = db.get(VoiceSipAccount, test.account_id)
            phone = test.target_phone
            account_id = test.account_id
            if not account:
                raise ValueError("SIP account unavailable")
            if not _guard(test_id, phone, account_id):
                raise ValueError("Pilot blocked")
            config = PilotConfig.from_env()
            prefix = None if account_route else config.prefix
            route_label = "NATIONAL" if account_route else config.prefix
            secret = decrypt_secret(account.password_encrypted)
            db.expunge(account)
        progress("Iniciando registro SIP.")
        with PjSession(account, secret) as driver:
            register_code, _ = driver.wait_for_registration()
            if register_code != 200:
                progress("Registro SIP falhou: codigo " + str(register_code) + ".")
                raise ValueError("SIP registration failed")
            progress("Registro SIP 200 OK.")
            def abort():
                refresh_heartbeat()
                return not _guard(test_id, phone, account_id)

            if probe:
                outcome = perform_native_probe(driver, phone, prefix,
                                               should_abort=abort, on_progress=progress)
            else:
                outcome = perform_lia_test(
                    driver, phone, prefix, os.getenv("LIA_VOZ_OPENAI_API_KEY", ""),
                    should_abort=abort, on_progress=progress,
                )
        logging.getLogger(__name__).info("Lia Voz teste %d: %s | %s",
                                         test_id, outcome.status, outcome.message)
        _finish(test_id, outcome.status, (route_label + " | " + outcome.message)[:230],
                outcome.sip_code, register_code)
    except Exception as exc:
        message = ("Falha (" + _safe_failure_kind(exc) + "). Ultima etapa: " + last_stage)[:230]
        logging.getLogger(__name__).warning("Lia Voz teste %d: %s", test_id, message)
        _finish(test_id, "LIA_FALHOU", message, sip_code=last_code, register_code=register_code)
