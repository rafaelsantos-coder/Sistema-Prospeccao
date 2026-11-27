"""Lia Voz single test queue, consumed only by an explicitly isolated voice engine."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import User, VoiceDialerEngine, VoiceSipAccount, VoiceTestCall
from app.services.lia_voz_call import perform_lia_test
from app.services.lia_voz_pilot import ACTIVE, PENDING, RUNNING, PilotConfig, ensure_idle
from app.services.secret_store import decrypt_secret
from app.services.voice_pjsua2 import PjSession
from app.services.voice_runtime import outbound_guard
from app.services.voice_test import aware


def _now():
    return datetime.now(timezone.utc)


def _enabled():
    # Never run in the same engine while commercial calling or legacy tests
    # are enabled. Other services/projects must also be isolated operationally.
    return (
        PilotConfig.from_env().configured
        and os.getenv("VOICE_CAMPAIGN_OUTBOUND_ENABLED", "").lower() != "true"
        and os.getenv("VOICE_TRANSFER_TESTS_ENABLED", "").lower() != "true"
        and bool(os.getenv("LIA_VOZ_OPENAI_API_KEY", "").strip())
    )


def _finish(test_id, status, message, sip_code=None, register_code=None):
    with SessionLocal() as db:
        row = db.get(VoiceTestCall, test_id)
        if row is None or row.status != RUNNING:
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
            if _now() - aware(row.created_at) > timedelta(minutes=4):
                row.status = "LIA_INDETERMINADO" if row.status == RUNNING else "LIA_EXPIRADO"
                row.finished_at = _now()
                row.message = "Teste expirado; nao repetir automaticamente."
        db.commit()


def claim():
    if not _enabled():
        return None
    with SessionLocal() as db:
        engine = db.scalar(select(VoiceDialerEngine).where(
            VoiceDialerEngine.id == 1
        ).with_for_update())
        if not engine or not engine.enabled:
            return None
        row = db.scalar(select(VoiceTestCall).where(
            VoiceTestCall.status == PENDING
        ).order_by(VoiceTestCall.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if _now() - aware(row.created_at) > timedelta(seconds=90):
            row.status = "LIA_EXPIRADO"
            row.finished_at = _now()
            row.message = "Solicitacao de teste expirou."
            db.commit()
            return None
        row.status = RUNNING
        row.started_at = _now()
        db.commit()
        return row.id


def _guard(test_id, phone, account_id):
    if not _enabled():
        return False
    try:
        with SessionLocal() as db:
            engine = db.get(VoiceDialerEngine, 1)
            account = db.get(VoiceSipAccount, account_id)
            test = db.get(VoiceTestCall, test_id)
            actor = db.get(User, test.initiator_user_id) if test and test.initiator_user_id else None
            if not (engine and engine.enabled and account and account.active
                    and test and test.status == RUNNING and actor and actor.active
                    and actor.role == "ADMIN" and test.account_id == account_id
                    and test.target_phone == phone):
                return False
            PilotConfig.from_env().authorize(phone, account.username)
            ensure_idle(db, own_test_id=test_id)
            outbound_guard(db, phone)
        return True
    except Exception:
        return False


def run(test_id):
    register_code = None
    try:
        with SessionLocal() as db:
            test = db.get(VoiceTestCall, test_id)
            if not test or test.status != RUNNING or not test.account_id:
                return
            account = db.get(VoiceSipAccount, test.account_id)
            phone = test.target_phone
            account_id = test.account_id
            if not account:
                raise ValueError("SIP account unavailable")
            if not _guard(test_id, phone, account_id):
                raise ValueError("Pilot blocked")
            config = PilotConfig.from_env()
            secret = decrypt_secret(account.password_encrypted)
            db.expunge(account)
        with PjSession(account, secret) as driver:
            register_code, _ = driver.wait_for_registration()
            if register_code != 200:
                raise ValueError("SIP registration failed")
            outcome = asyncio.run(perform_lia_test(
                driver, phone, config.prefix, os.getenv("LIA_VOZ_OPENAI_API_KEY", ""),
                should_abort=lambda: not _guard(test_id, phone, account_id),
            ))
        _finish(test_id, outcome.status, outcome.message, outcome.sip_code, register_code)
    except Exception:
        # Never persist raw API/SIP errors, phone, transcript, or secrets.
        _finish(test_id, "LIA_FALHOU", "Teste bloqueado ou falha SIP/Realtime/midia.",
                register_code=register_code)
