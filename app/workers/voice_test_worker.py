"""Isolated Prospecta VOZ diagnostic worker.

Only admin-initiated one-number test calls are processed. No campaign calls.
Run as exactly one Railway replica, after adding DB/cryptography environment refs.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal
from app.models import VoiceSipAccount, VoiceTestAgent, VoiceTestCall
from app.services.secret_store import SecretStoreError, decrypt_secret
from app.services.voice_sip_probe import SipDiagnostic, SipProbe, SipProbeError
from app.services.voice_test import aware, now_utc, REQUEST_MAX_AGE
from app.services.voice_runtime import outbound_guard, update_sip_check
from app.services.voice_execution_slot import voice_execution_slot, slot_healthy

logger = logging.getLogger(__name__)
WORKER_ID = (socket.gethostname() + "-" + uuid4().hex[:8])[:120]


def heartbeat() -> None:
    with SessionLocal() as db:
        row = db.get(VoiceTestAgent, 1)
        if not row:
            row = VoiceTestAgent(id=1, worker_id=WORKER_ID)
            db.add(row)
        row.worker_id = WORKER_ID
        row.heartbeat_at = now_utc()
        db.commit()


def _record(test_id: int, status: str, register_code: int | None,
            invite_code: int | None, message: str, terminal: bool = False) -> None:
    with SessionLocal() as db:
        test = db.get(VoiceTestCall, test_id)
        if not test:
            return
        test.status = status
        if register_code is not None:
            test.sip_register_code = register_code
        if invite_code is not None:
            test.sip_invite_code = invite_code
        test.message = message[:230]
        if terminal:
            test.finished_at = now_utc()
        db.commit()


def claim_next() -> tuple[int, int, str] | None:
    with SessionLocal() as db:
        # On PostgreSQL a row lock prevents two workers from claiming the same test.
        candidate = db.scalar(
            select(VoiceTestCall).where(VoiceTestCall.status == "PENDENTE")
            .order_by(VoiceTestCall.id.asc()).with_for_update(skip_locked=True).limit(1)
        )
        if not candidate:
            return None
        now = now_utc()
        submitted = aware(candidate.created_at)
        if submitted and now - submitted > REQUEST_MAX_AGE:
            candidate.status = "EXPIRADO"
            candidate.message = "Teste expirado. Nenhuma ligacao foi feita."
            candidate.finished_at = now
            db.commit()
            return None
        if not candidate.account_id:
            candidate.status = "FALHOU"
            candidate.message = "Conta SIP removida."
            candidate.finished_at = now
            db.commit()
            return None
        account_id = candidate.account_id
        candidate.status = "REGISTRANDO"
        candidate.started_at = now
        candidate.message = "Agente SIP iniciando verificacao."
        db.commit()
        return candidate.id, account_id, candidate.target_phone


def run_one(test_id: int, account_id: int, phone: str) -> None:
    try:
        with SessionLocal() as db:
            account = db.get(VoiceSipAccount, account_id)
            if not account or not account.active:
                _record(test_id, "FALHOU", None, None, "Conta SIP pausada ou removida.", True)
                return
            if account.transport != "UDP":
                _record(test_id, "FALHOU", None, None, "Transporte nao homologado; teste UDP somente.", True)
                return
            host = account.domain
            sip_port = account.sip_port
            proxy_host = account.proxy_host
            proxy_port = account.proxy_port
            username = account.username
            password = decrypt_secret(account.password_encrypted)
            if phone != "SIP_ONLY":
                outbound_guard(db, phone)
        if not password:
            _record(test_id, "FALHOU", None, None, "Credencial SIP ausente.", True)
            return
        is_register_only = phone == "SIP_ONLY"
        probe = SipProbe(
            host=host, username=username, password=password,
            phone=None if is_register_only else phone,
            port=sip_port, proxy_host=proxy_host, proxy_port=proxy_port,
            notify=lambda status, register, invite, message:
                _record(test_id, status, register, invite, message),
        )
        if not slot_healthy():
            _record(test_id, "FALHOU", None, None, "Trava de execucao SIP indisponivel.", True)
            return
        result: SipDiagnostic = probe.run_registration_only() if is_register_only else probe.run()
        if result.register_code is not None:
            with SessionLocal() as db:
                record = db.get(VoiceSipAccount, account_id)
                if record:
                    update_sip_check(db, record, result.register_code, result.message)
                    db.commit()
        _record(test_id, result.status, result.register_code, result.invite_code,
                result.message, True)
    except (SecretStoreError, SipProbeError) as exc:
        # Never log authentication strings or raw SIP messages.
        if isinstance(exc, SipProbeError):
            if exc.register_code is not None:
                with SessionLocal() as db:
                    account = db.get(VoiceSipAccount, account_id)
                    if account:
                        update_sip_check(db, account, exc.register_code, str(exc))
                        db.commit()
            _record(test_id, "FALHOU", exc.register_code, exc.invite_code, str(exc), True)
        else:
            _record(test_id, "FALHOU", None, None, "Chave de criptografia do agente incorreta.", True)
    except Exception:
        logger.exception("Falha inesperada no teste SIP id=%s (sem revelar credenciais)", test_id)
        _record(test_id, "FALHOU", None, None, "Falha interna do agente SIP. Verifique o log.", True)


def expire_stale() -> None:
    # Avoid redialing after a crash; a once-started test is never automatically resumed.
    with SessionLocal() as db:
        deadline = now_utc() - timedelta(minutes=2)
        tests = list(db.scalars(select(VoiceTestCall).where(
            VoiceTestCall.status.in_(["REGISTRANDO", "REGISTRADO", "CHAMANDO", "TOCANDO"])
        )))
        for test in tests:
            started = aware(test.started_at)
            if started and started < deadline:
                test.status = "FALHOU"
                test.message = "Agente foi interrompido; nao houve nova tentativa automatica."
                test.finished_at = now_utc()
        db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if os.getenv("VOICE_TEST_WORKER_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("VOICE_TEST_WORKER_ENABLED nao configurado; agente SIP inativo.")
    logger.info("Prospecta VOZ: agente SIP de testes iniciado, sem campanhas automaticas.")
    last_heartbeat = 0.0
    last_recovery = 0.0
    while True:
        try:
            moment = time.monotonic()
            if moment - last_heartbeat > 5:
                heartbeat()
                last_heartbeat = moment
            with voice_execution_slot() as acquired:
                if acquired:
                    if moment - last_recovery > 30:
                        expire_stale()
                        last_recovery = moment
                    item = claim_next()
                    if item:
                        run_one(*item)
                        last_heartbeat = 0
        except Exception:
            logger.exception("Erro no loop isolado de testes SIP")
        time.sleep(2)


if __name__ == "__main__":
    main()
