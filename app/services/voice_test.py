"""Safe helpers for explicit, single-number SIP registration/dial diagnostics."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import VoiceTestAgent, VoiceTestCall, VoiceSipAccount
from app.services.global_blacklist import is_globally_blocked

TERMINAL = {"TOCOU", "ATENDIDA", "OCUPADO", "FALHOU", "SEM_RESPOSTA", "EXPIRADO", "SIP_VALIDADO"}
ACTIVE = {"PENDENTE", "REGISTRANDO", "REGISTRADO", "CHAMANDO", "TOCANDO"}
BRAZIL_DDDS = {11,12,13,14,15,16,17,18,19,21,22,24,27,28,31,32,33,34,35,37,38,
               41,42,43,44,45,46,47,48,49,51,53,54,55,61,62,63,64,65,66,
               67,68,69,71,73,74,75,77,79,81,82,83,84,85,86,87,88,89,
               91,92,93,94,95,96,97,98,99}
AGENT_MAX_AGE = timedelta(seconds=45)
REQUEST_MAX_AGE = timedelta(seconds=90)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_agent_online(db: Session, now: datetime | None = None) -> bool:
    agent = db.get(VoiceTestAgent, 1)
    beat = aware(agent.heartbeat_at) if agent else None
    return bool(beat and (now or now_utc()) - beat <= AGENT_MAX_AGE)


def validate_test_phone(value: str) -> str:
    raw = (value or "").strip()
    if not re.fullmatch(r"\+?[0-9 ()\-.]{10,24}", raw):
        raise ValueError("Informe um telefone brasileiro valido com DDD.")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55") and len(digits) in (12, 13):
        national = digits[2:]
    else:
        national = digits
    if len(national) not in (10, 11) or int(national[:2]) not in BRAZIL_DDDS:
        raise ValueError("Use um numero com DDD (10 ou 11 digitos), opcionalmente prefixado com 55.")
    # Envia o numero na forma digitada, sem pontuacao; o PABX determina o plano de discagem.
    return digits


def verify_test_allowed(db: Session, account: VoiceSipAccount, phone: str, now: datetime | None = None) -> None:
    at = now or now_utc()
    if not account.active:
        raise ValueError("Ative a conta SIP antes de testar.")
    if account.transport != "UDP":
        raise ValueError("Nesta fase, o teste real de chamada suporta somente contas SIP UDP.")
    if not is_agent_online(db, at):
        raise ValueError("Agente SIP de testes indisponivel. Nenhuma chamada foi iniciada.")
    # Every voice channel shares the same opt-out blacklist. SIP_ONLY is no call.
    if phone != "SIP_ONLY":
        try:
            if is_globally_blocked(db, phone):
                raise ValueError("Este telefone esta na blacklist global. Ligacao bloqueada.")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Nao foi possivel verificar a blacklist. Chamada bloqueada.") from exc
    # Force sequential tests per SIP account to avoid account registration conflicts.
    active = db.scalar(select(func.count(VoiceTestCall.id)).where(VoiceTestCall.status.in_(ACTIVE))) or 0
    if active:
        raise ValueError("Existe outro teste de voz em andamento. Finalize antes de iniciar outro.")
    count = db.scalar(select(func.count(VoiceTestCall.id)).where(
        VoiceTestCall.account_id == account.id,
        VoiceTestCall.created_at >= at - timedelta(hours=1)
    )) or 0
    if count >= 3:
        raise ValueError("Limite de seguranca: tres verificacoes SIP ou ligacoes por conta a cada hora.")
    repeat = db.scalar(select(VoiceTestCall.id).where(
        VoiceTestCall.account_id == account.id,
        VoiceTestCall.target_phone == phone,
        VoiceTestCall.created_at >= at - timedelta(minutes=3)
    ).limit(1))
    if repeat:
        raise ValueError("Aguarde pelo menos tres minutos para testar novamente o mesmo numero.")
