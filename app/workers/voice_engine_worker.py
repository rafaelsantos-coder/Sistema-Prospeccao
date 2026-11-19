"""Opt-in, single-call-at-a-time campaign/transfer engine using native PJSUA2.

Runs in a separate Python 3.12 container. No autonomous customer calling unless
both Railway VOICE_CAMPAIGN_OUTBOUND_ENABLED and admin kill-switch allow it,
all readiness checks pass, and an ADMIN explicitly starts each campaign.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    VoiceCallEvent, VoiceCampaign, VoiceDialerEngine, VoiceGroup, VoiceRecipient,
    VoiceSipAccount, VoiceTestCall, VoiceTransferTest,
)
from app.services.global_blacklist import is_globally_blocked
from app.services.secret_store import decrypt_secret
from app.services.voice_pjsua2 import PjSession, VoiceEngineError
from app.services.voice_runtime import (
    MAX_ACTIVE_CALLS_PER_INSTANCE, outbound_guard, check_campaign_ready,
    next_attempt_allowed, update_sip_check,
)
from app.services.voice_test import aware
from app.workers.graceful_shutdown import GracefulShutdown
from app.workers.voice_singleton import VoiceLeaderLease, VoiceLeadershipLost, voice_engine_leadership

LOG = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}-{uuid4().hex[:9]}"[:120]
CAMPAIGNS_ENABLED = os.getenv("VOICE_CAMPAIGN_OUTBOUND_ENABLED", "").lower() == "true"
TRANSFER_TESTS_ENABLED = os.getenv("VOICE_TRANSFER_TESTS_ENABLED", "").lower() == "true"


def now() -> datetime:
    return datetime.now(timezone.utc)


def heartbeat(error: str = "") -> None:
    with SessionLocal() as db:
        engine = db.get(VoiceDialerEngine, 1)
        if engine is None:
            raise RuntimeError("Migracao voice_dialer_engine nao executada.")
        engine.heartbeat_at = now()
        engine.worker_id = WORKER_ID
        engine.backend = "PJSUA2"
        engine.campaign_runtime_enabled = CAMPAIGNS_ENABLED
        engine.test_runtime_enabled = TRANSFER_TESTS_ENABLED
        engine.last_error = (error or "")[:200]
        db.commit()


def engine_enabled() -> bool:
    with SessionLocal() as db:
        engine = db.get(VoiceDialerEngine, 1)
        return bool(engine and engine.enabled)


def outbound_allowed() -> bool:
    """Emergency switch is read between calls; no automatic retries."""
    return engine_enabled()


def _load_wav(s3_uri: str) -> str:
    # No requests to arbitrary URLs or untrusted hosts.
    if not s3_uri.startswith("s3://"):
        raise VoiceEngineError("Audio fora do armazenamento privado.")
    bucket, _, key = s3_uri[5:].partition("/")
    if not key or bucket != settings.media_s3_bucket:
        raise VoiceEngineError("Bucket da gravacao nao corresponde ao cadastrado.")
    if not all([
        settings.media_s3_endpoint, settings.media_s3_access_key_id,
        settings.media_s3_secret_access_key,
    ]):
        raise VoiceEngineError("Credenciais de midia nao configuradas para worker VOZ.")
    import boto3
    client = boto3.client(
        "s3", endpoint_url=settings.media_s3_endpoint,
        region_name=settings.media_s3_region or "auto",
        aws_access_key_id=settings.media_s3_access_key_id,
        aws_secret_access_key=settings.media_s3_secret_access_key,
    )
    obj = client.get_object(Bucket=bucket, Key=key)
    # Teto técnico configurável, compartilhado com o armazenamento de mídia da web.
    max_bytes = max(1, settings.media_max_upload_mb) * 1024 * 1024
    data = obj["Body"].read(max_bytes + 1)
    from app.services.voice import validate_wav
    validate_wav(data, max_bytes=max_bytes)
    fd, path = tempfile.mkstemp(prefix="voice-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return path
    except Exception:
        os.close(fd)
        raise


def _set_transfer(test_id: int, status: str, code: int | None, message: str, final: bool = False):
    with SessionLocal() as db:
        row = db.get(VoiceTransferTest, test_id)
        if row:
            row.result = status[:35]
            row.sip_code = code
            row.message = (message or "")[:200]
            if final:
                row.finished_at = now()
            db.commit()


def _set_recipient(recipient_id: int, status: str, code: int | None,
                   message: str, final: bool = False, answered: bool = False,
                   transferred: bool = False):
    with SessionLocal() as db:
        row = db.get(VoiceRecipient, recipient_id)
        if row:
            row.status = status[:25]
            row.last_event = (status or "")[:40]
            row.last_error = (message or "")[:200] if status not in {"TRANSFERIDA", "ATENDIDA", "AUDIO_CONCLUIDO"} else ""
            if answered:
                row.answered_at = now()
            if transferred:
                row.transferred_at = now()
            db.add(VoiceCallEvent(
                recipient_id=row.id, event_id=uuid4().hex,
                event=(status or "INDETERMINADO")[:35],
                detail=(message or f"SIP {code or '-'}")[:180],
            ))
            db.commit()


def mark_registration(account_id: int, code: int | None, message: str):
    with SessionLocal() as db:
        account = db.get(VoiceSipAccount, account_id)
        if account:
            update_sip_check(db, account, code, message)
            db.commit()


def _claim_native_test() -> int | None:
    if not TRANSFER_TESTS_ENABLED or not outbound_allowed():
        return None
    with SessionLocal() as db:
        test = db.scalar(select(VoiceTestCall).where(
            VoiceTestCall.status == "PENDENTE_PJSIP"
        ).order_by(VoiceTestCall.id).with_for_update(skip_locked=True).limit(1))
        if not test:
            return None
        if now() - aware(test.created_at) > timedelta(minutes=3):
            test.status = "EXPIRADO"
            test.message = "Teste SIP/RTP expirou; nenhuma ligacao foi iniciada."
            test.finished_at = now()
            db.commit()
            return None
        test.status = "REGISTRANDO_PJSIP"
        test.started_at = now()
        db.commit()
        return test.id


def _native_record(test_id: int, status: str, register_code: int | None,
                   invite_code: int | None, message: str, *, final=False) -> None:
    with SessionLocal() as db:
        test = db.get(VoiceTestCall, test_id)
        if not test:
            return
        test.status = status[:30]
        if register_code is not None:
            test.sip_register_code = register_code
        if invite_code is not None:
            test.sip_invite_code = invite_code
        test.message = (message or "")[:230]
        if final:
            test.finished_at = now()
        db.commit()


def _run_native_test(test_id: int, leader: VoiceLeaderLease):
    try:
        with SessionLocal() as db:
            test = db.get(VoiceTestCall, test_id)
            if not test or test.status != "REGISTRANDO_PJSIP" or not test.account_id:
                return
            account = db.get(VoiceSipAccount, test.account_id)
            if not account or not account.active:
                raise VoiceEngineError("Conta SIP nao esta ativa.")
            number = test.target_phone
            outbound_guard(db, number)
            secret = decrypt_secret(account.password_encrypted)
            account_id = account.id
            db.expunge(account)
        with PjSession(account, secret) as driver:
            code, message = driver.wait_for_registration()
            mark_registration(account_id, code, message)
            _native_record(test_id, "REGISTRADO" if code == 200 else "FALHOU", code,
                           None, message)
            if code != 200:
                raise VoiceEngineError(message)
            with SessionLocal() as db:
                outbound_guard(db, number)
            if not outbound_allowed():
                raise VoiceEngineError("Chamada interrompida antes de discar.")
            leader.assert_held(force=True)
            result = driver.perform_call(
                number, mode="PROBE",
                on_status=lambda status, sip: _native_record(
                    test_id, status, 200, sip, f"Teste PJSIP: {status}"
                ),
                should_abort=lambda: not outbound_allowed() or not leader.healthy(),
                timeout=30,
            )
            detail = result.message + (
                " RTP negociado com midia ativa." if result.media_active
                else " Audio RTP nao confirmado."
            )
            _native_record(test_id, result.status, 200, result.sip_code, detail, final=True)
    except Exception as exc:
        LOG.error("Falha em teste nativo %s: %s", test_id, type(exc).__name__)
        _native_record(test_id, "FALHOU", None, None, str(exc), final=True)


def _claim_transfer_test() -> int | None:
    if not TRANSFER_TESTS_ENABLED or not outbound_allowed():
        return None
    with SessionLocal() as db:
        row = db.scalar(select(VoiceTransferTest).where(
            VoiceTransferTest.result == "PENDENTE"
        ).order_by(VoiceTransferTest.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if now() - aware(row.created_at) > timedelta(minutes=3):
            row.result = "EXPIRADO"
            row.message = "Solicitacao de teste expirou sem executar."
            row.finished_at = now()
            db.commit()
            return None
        row.result = "REGISTRANDO"
        db.commit()
        return row.id


def _run_transfer_test(test_id: int, leader: VoiceLeaderLease) -> None:
    try:
        with SessionLocal() as db:
            row = db.get(VoiceTransferTest, test_id)
            if not row:
                return
            account = db.get(VoiceSipAccount, row.account_id)
            group = db.get(VoiceGroup, row.group_id)
            if not account or not group or not account.active or not group.active:
                raise VoiceEngineError("Conta ou grupo desativado.")
            phone = decrypt_secret(row.phone_encrypted)
            secret = decrypt_secret(account.password_encrypted)
            outbound_guard(db, phone)
            aid, gid = account.id, group.id
            # Data materialized only for the bounded native SIP session.
            db.expunge(account)
            db.expunge(group)
        with PjSession(account, secret) as driver:
            code, message = driver.wait_for_registration()
            mark_registration(aid, code, message)
            if code != 200:
                raise VoiceEngineError(message)
            # Recheck opt-out immediately before INVITE; fail-closed.
            with SessionLocal() as db:
                outbound_guard(db, phone)
            if not outbound_allowed():
                raise VoiceEngineError("Discador pausado antes da chamada.")
            leader.assert_held(force=True)
            result = driver.perform_call(
                phone, mode="DIRECT", group=group,
                on_status=lambda status, code: _set_transfer(
                    test_id, status, code, f"Teste {status.lower()}"),
                should_abort=lambda: not outbound_allowed() or not leader.healthy(),
            )
            _set_transfer(test_id, result.status, result.sip_code, result.message, True)
            if result.transferred:
                with SessionLocal() as db:
                    grp = db.get(VoiceGroup, gid)
                    if grp and grp.active:
                        grp.transfer_verified_at = now()
                        db.commit()
    except Exception as exc:
        LOG.error("Falha de transferencia de teste id=%s: %s", test_id, type(exc).__name__)
        _set_transfer(test_id, "FALHOU", None, str(exc), True)


def _claim_recipient() -> int | None:
    if not CAMPAIGNS_ENABLED or not outbound_allowed():
        return None
    # Safe default: no predictive dialing, one customer call at any moment.
    with SessionLocal() as db:
        campaigns = list(db.scalars(select(VoiceCampaign).where(
            VoiceCampaign.status == "EM_EXECUCAO"
        ).order_by(VoiceCampaign.id.asc())))
        for campaign in campaigns:
            # Concluir independentemente da janela, caso a última chamada tenha
            # sido processada. Não concluir enquanto outra ligação estiver ativa.
            pending = db.scalar(select(VoiceRecipient.id).where(
                VoiceRecipient.campaign_id == campaign.id,
                VoiceRecipient.status == "PENDENTE",
                VoiceRecipient.attempts == 0,
            ).limit(1))
            if pending is None:
                active_call = db.scalar(select(VoiceRecipient.id).where(
                    VoiceRecipient.campaign_id == campaign.id,
                    VoiceRecipient.status.in_(
                        ("INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO")
                    ),
                ).limit(1))
                if active_call is None:
                    campaign.status = "CONCLUIDA"
                    campaign.stopped_at = now()
                    db.commit()
                continue
            if check_campaign_ready(db, campaign):
                continue
            if not next_attempt_allowed(db, campaign):
                continue
            item = db.scalar(select(VoiceRecipient).where(
                VoiceRecipient.campaign_id == campaign.id,
                VoiceRecipient.status == "PENDENTE",
                VoiceRecipient.attempts == 0,
            ).order_by(VoiceRecipient.id.asc()).with_for_update(skip_locked=True).limit(1))
            if item is None:
                continue
            try:
                outbound_guard(db, item.phone)
            except ValueError as exc:
                item.status = "BLOQUEADA"
                item.last_error = str(exc)[:200]
                db.commit()
                continue
            item.status = "INICIANDO"
            item.attempts = 1
            item.last_attempt_at = now()
            item.dial_token = uuid4().hex
            campaign.last_call_at = now()
            db.commit()
            return item.id
    return None


def campaign_call_permitted(recipient_id: int) -> bool:
    """Stop an in-flight call immediately if ADMIN paused the campaign or engine."""
    if not outbound_allowed():
        return False
    try:
        with SessionLocal() as db:
            row = db.get(VoiceRecipient, recipient_id)
            campaign = db.get(VoiceCampaign, row.campaign_id) if row else None
            return bool(campaign and campaign.status == "EM_EXECUCAO")
    except Exception:
        return False


def _run_campaign_call(recipient_id: int, leader: VoiceLeaderLease) -> None:
    file_path = None
    try:
        with SessionLocal() as db:
            item = db.get(VoiceRecipient, recipient_id)
            if not item or item.status != "INICIANDO":
                return
            campaign = db.get(VoiceCampaign, item.campaign_id)
            if not campaign or campaign.status != "EM_EXECUCAO" or check_campaign_ready(db, campaign, require_pending=False):
                raise VoiceEngineError("Campanha ficou indisponivel para discagem.")
            account = db.get(VoiceSipAccount, campaign.sip_account_id)
            group = db.get(VoiceGroup, campaign.group_id) if campaign.group_id is not None else None
            if not account or (campaign.group_id is not None and not group):
                raise VoiceEngineError("Conta SIP ou grupo removido.")
            if campaign.group_id is None and campaign.mode != "AUDIO":
                raise VoiceEngineError("Campanha sem grupo deve utilizar áudio informativo.")
            phone = item.phone
            outbound_guard(db, phone)
            secret = decrypt_secret(account.password_encrypted)
            mode, dtmf, audio_uri = campaign.mode, campaign.dtmf_digit, campaign.audio_uri
            dial_format = campaign.dial_format or "NATIONAL"
            aid = account.id
            db.expunge(account)
            if group is not None:
                db.expunge(group)
        if mode == "AUDIO":
            file_path = _load_wav(audio_uri)
        with PjSession(account, secret) as driver:
            code, msg = driver.wait_for_registration()
            mark_registration(aid, code, msg)
            if code != 200:
                raise VoiceEngineError(msg)
            # Exact last-millisecond check before placing the call.
            with SessionLocal() as db:
                outbound_guard(db, phone)
                fresh = db.get(VoiceRecipient, recipient_id)
                campaign = db.get(VoiceCampaign, fresh.campaign_id) if fresh else None
                if not campaign or campaign.status != "EM_EXECUCAO":
                    raise VoiceEngineError("Campanha interrompida antes de discar.")
            if not outbound_allowed():
                raise VoiceEngineError("Discador interrompido.")
            leader.assert_held(force=True)
            result = driver.perform_call(
                phone, mode=mode, group=group,
                audio_wav=file_path, dtmf_digit=dtmf, dial_format=dial_format,
                on_status=lambda status, code: _set_recipient(
                    recipient_id, status, code, ""),
                should_abort=lambda: not campaign_call_permitted(recipient_id) or not leader.healthy(),
            )
            _set_recipient(recipient_id, result.status, result.sip_code, result.message,
                           True, result.answered, result.transferred)
    except Exception as exc:
        LOG.error("Falha de chamada voz recipient_id=%s: %s", recipient_id, type(exc).__name__)
        _set_recipient(recipient_id, "FALHOU", None, str(exc), True)
    finally:
        if file_path:
            Path(file_path).unlink(missing_ok=True)


def _expire_crashed_calls() -> None:
    """Never redial calls that might have been placed before a worker restart."""
    with SessionLocal() as db:
        for test in db.scalars(select(VoiceTestCall).where(
            VoiceTestCall.status.in_(["REGISTRANDO_PJSIP", "PENDENTE_PJSIP"])
        )):
            if now() - aware(test.created_at) > timedelta(minutes=3):
                test.status = "INDETERMINADO"
                test.message = "Worker reiniciou; teste nativo nao sera repetido."
                test.finished_at = now()
        for row in db.scalars(select(VoiceTransferTest).where(
            VoiceTransferTest.result.in_([
                "REGISTRANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO",
            ])
        )):
            if now() - aware(row.created_at) > timedelta(minutes=3):
                row.result = "INDETERMINADO"
                row.message = "Worker reiniciou; nao repetir ligacao automaticamente."
                row.finished_at = now()
        for row in db.scalars(select(VoiceRecipient).where(
            VoiceRecipient.status.in_(["INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO"])
        )):
            if row.last_attempt_at and now() - aware(row.last_attempt_at) > timedelta(minutes=3):
                row.status = "INDETERMINADO"
                row.last_error = "Estado apos falha do worker. Sem rediscagem automatica."
        db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if os.getenv("VOICE_ENGINE_WORKER_ENABLED", "").lower() != "true":
        raise SystemExit("VOICE_ENGINE_WORKER_ENABLED=false: motor de campanhas permanece desativado.")
    shutdown = GracefulShutdown(LOG)
    shutdown.install()
    LOG.info(
        "Prospecta VOZ PJSUA2 iniciado. Campanhas=%s, testes de transferencia=%s.",
        CAMPAIGNS_ENABLED, TRANSFER_TESTS_ENABLED,
    )

    # Em deploy com overlap, a nova replica aguarda sem discar enquanto a antiga
    # mantem o lock de sessao no PostgreSQL. Apenas o lider registra heartbeat.
    standby_logged = False
    try:
        while not shutdown.requested:
            with voice_engine_leadership() as leader:
                if leader is None:
                    if not standby_logged:
                        LOG.warning("Motor SIP em standby: outra instancia possui lock exclusivo.")
                        standby_logged = True
                    shutdown.wait(3)
                    continue

                standby_logged = False
                LOG.info("Motor SIP adquiriu lideranca exclusiva no PostgreSQL.")
                leader.assert_held(force=True)
                _expire_crashed_calls()
                last_beat = 0.0
                while not shutdown.requested:
                    # Falha de conexao revoga imediatamente autorizacao para novos claims.
                    leader.assert_held()
                    try:
                        t = time.monotonic()
                        if t - last_beat >= 8:
                            heartbeat()
                            last_beat = t
                        if shutdown.requested:
                            break

                        native_id = _claim_native_test()
                        if native_id is not None:
                            leader.assert_held(force=True)
                            _run_native_test(native_id, leader)
                            last_beat = 0
                        elif not shutdown.requested and (test_id := _claim_transfer_test()) is not None:
                            leader.assert_held(force=True)
                            _run_transfer_test(test_id, leader)
                            last_beat = 0
                        elif not shutdown.requested and (recipient_id := _claim_recipient()) is not None:
                            leader.assert_held(force=True)
                            # Uma chamada ativa continua ate o encerramento/aborto seguro.
                            _run_campaign_call(recipient_id, leader)
                            last_beat = 0
                        else:
                            shutdown.wait(2)
                    except VoiceLeadershipLost:
                        raise
                    except Exception as exc:
                        LOG.exception("Falha no loop do motor de voz")
                        try:
                            heartbeat(type(exc).__name__)
                        except Exception:
                            pass
                        shutdown.wait(4)

                # O SIGTERM pediu parada: liberar sessao apenas depois da chamada
                # atual voltar do driver e do seu processamento.
                break
    except VoiceLeadershipLost:
        LOG.critical("Motor SIP perdeu lideranca; sair sem originar novas chamadas.", exc_info=True)
        raise SystemExit(1) from None
    except Exception:
        LOG.critical("Motor SIP falhou no lock exclusivo; encerrar por seguranca.", exc_info=True)
        raise SystemExit(1) from None

    LOG.info("Motor de voz encerrado apos processamento em andamento.")


if __name__ == "__main__":
    main()
