"""Fail-closed readiness, routing and throttles for Prospecta VOZ.

This module does not place calls. The PJSUA2 worker must invoke outbound_guard
again immediately before each external SIP INVITE, independent of the UI state.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    VoiceCampaign, VoiceDialerEngine, VoiceGroup, VoiceRecipient,
    VoiceSipAccount, VoiceTestCall,
)
from app.services.global_blacklist import is_globally_blocked
from app.services.voice_test import aware, validate_test_phone

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
ENGINE_HEARTBEAT_MAX = timedelta(seconds=65)
SIP_VERIFICATION_MAX = timedelta(hours=1)
MAX_CALLS_PER_DAY = 30
MIN_SECONDS_BETWEEN_CALLS = 60
MAX_ACTIVE_CALLS_PER_INSTANCE = 1

_HOST_RE = re.compile(r"^(?=.{1,190}$)[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$")
_EXT_RE = re.compile(r"^[0-9*#]{1,20}$")


class VoiceReadinessError(ValueError):
    pass


def validate_host(value: str, *, optional: bool = False) -> str:
    host = (value or "").strip().lower()
    if optional and not host:
        return ""
    try:
        address = ipaddress.ip_address(host)
        if address.version != 4:
            raise ValueError("Somente IPv4 e nomes DNS sao suportados inicialmente.")
        return host
    except ValueError as exc:
        if ":" in host:
            raise ValueError("IP ou dominio SIP invalido.") from exc
    if not _HOST_RE.fullmatch(host) or ".." in host:
        raise ValueError("Servidor SIP invalido. Informe IPv4 ou dominio DNS, sem protocolo.")
    return host


def validate_port(value: int) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Porta SIP deve estar entre 1 e 65535.")
    return port


def validate_routing_options(
    sip_port: int = 5060, proxy_host: str = "", proxy_port: int = 5060,
    dial_domain: str = "", dial_prefix: str = "", caller_id: str = "",
) -> dict:
    prefix = (dial_prefix or "").strip()
    if not re.fullmatch(r"[0-9]{0,12}", prefix):
        raise ValueError("Prefixo de discagem: use ate 12 digitos.")
    caller = (caller_id or "").strip()
    if caller and not re.fullmatch(r"[0-9+]{1,50}", caller):
        raise ValueError("Identificador de chamada invalido.")
    return {
        "sip_port": validate_port(sip_port),
        "proxy_host": validate_host(proxy_host, optional=True),
        "proxy_port": validate_port(proxy_port),
        "dial_domain": validate_host(dial_domain, optional=True),
        "dial_prefix": prefix, "caller_id": caller,
    }


def make_dial_uri(account: VoiceSipAccount, number: str, *,
                  dial_format: str | None = None) -> str:
    """Monta o destino SIP segundo o plano de discagem; nunca duplica DDI.

    CRM/CSV guarda E.164 para deduplicacao/blacklist. Campanhas usam NATIONAL
    (DDD+numero) por padrao; testes SIP sem override mantem ajuste da conta.
    """
    clean = validate_test_phone(number)
    if dial_format is not None:
        if dial_format not in {"NATIONAL", "E164"}:
            raise VoiceReadinessError("Formato de discagem SIP invalido.")
        if dial_format == "NATIONAL" and len(clean) in (12, 13) and clean.startswith("55"):
            clean = clean[2:]
        elif dial_format == "E164" and len(clean) in (10, 11):
            clean = "55" + clean
    elif account.strip_brazil_ddi and clean.startswith("55") and len(clean) in (12, 13):
        clean = clean[2:]
    prefix = getattr(account, "dial_prefix", "") or ""
    if not re.fullmatch(r"[0-9]{0,12}", prefix):
        raise VoiceReadinessError("Prefixo SIP incorreto.")
    host = validate_host(account.dial_domain or account.domain)
    validate_port(account.sip_port)
    return f"sip:{prefix}{clean}@{host}:{account.sip_port};transport={account.transport.lower()}"


def make_transfer_uri(account: VoiceSipAccount, group: VoiceGroup) -> str:
    extension = (group.target_extension or "").strip()
    if not _EXT_RE.fullmatch(extension):
        raise VoiceReadinessError("Ramal de destino invalido.")
    host = validate_host(account.dial_domain or account.domain)
    return f"sip:{extension}@{host}:{account.sip_port};transport={account.transport.lower()}"


def outbound_guard(db: Session, phone: str) -> None:
    """Fail closed if blacklist cannot be queried. Applies to test and campaign."""
    clean = validate_test_phone(phone)
    try:
        if is_globally_blocked(db, clean):
            raise VoiceReadinessError("Telefone presente na blacklist global.")
    except VoiceReadinessError:
        raise
    except Exception as exc:
        raise VoiceReadinessError("Falha ao consultar blacklist global. Chamada suspensa.") from exc


def check_campaign_ready(db: Session, campaign: VoiceCampaign, at: datetime | None = None, *, require_pending: bool = True) -> list[str]:
    """All unmet prerequisites; never assume ACTIVE means the SIP account is online."""
    now = at or datetime.now(timezone.utc)
    errors: list[str] = []
    engine = db.get(VoiceDialerEngine, 1)
    if not engine or not engine.enabled:
        errors.append("Discador de campanhas esta pausado pelo administrador.")
    if not engine or not engine.campaign_runtime_enabled:
        errors.append("Worker de campanhas SIP ainda nao foi habilitado no Railway.")
    if not engine or not engine.heartbeat_at or (
        now - aware(engine.heartbeat_at) > ENGINE_HEARTBEAT_MAX
    ):
        errors.append("Motor de telefonia PJSUA2 indisponivel.")
    account = db.get(VoiceSipAccount, campaign.sip_account_id)
    if not account or not account.active:
        errors.append("Conta SIP ausente ou pausada.")
    elif not account.username or not account.domain or not account.password_encrypted:
        errors.append("Conta SIP incompleta: configure usuario, servidor e senha.")
    # Nao bloquear pela validade de um REGISTER antigo: o worker executa
    # REGISTER real em cada chamada e exige codigo SIP 200 ANTES do INVITE.
    # Assim, campanhas agendadas para o dia seguinte retomam automaticamente,
    # mantendo a autenticacao real como condicao de cada discagem.
    if campaign.group_id is None:
        # O modo AUDIO informativo encerra sem transferir a chamada.
        if campaign.mode != "AUDIO":
            errors.append("Transferencia direta exige um grupo de ramais ativo.")
    else:
        # Execucao real: utiliza o grupo cadastrado e registra o retorno SIP REFER
        # por ligacao. Nao exige teste de transferencia anterior nem certificado.
        group = db.get(VoiceGroup, campaign.group_id)
        if not group or not group.active:
            errors.append("Grupo de ramais ausente ou pausado.")
    if not campaign.legal_basis_verified:
        errors.append("Base legal e autorizacao de contato nao foram verificadas.")
    if campaign.mode == "AUDIO" and not campaign.audio_uri:
        errors.append("Falta audio WAV para a campanha.")
    pending = db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.status == "PENDENTE"
    )) or 0
    if require_pending and pending < 1:
        errors.append("Nao ha contatos pendentes na campanha.")
    return errors


def within_call_window(campaign: VoiceCampaign, at: datetime | None = None) -> bool:
    """Respeita exclusivamente a janela de horario cadastrada na campanha."""
    local = (at or datetime.now(timezone.utc)).astimezone(SAO_PAULO)
    current = local.strftime("%H:%M")
    return campaign.window_start <= current < campaign.window_end


def next_call_window_start(campaign: VoiceCampaign, at: datetime | None = None) -> datetime | None:
    """Proxima abertura com base apenas na janela configurada na campanha."""
    local = (at or datetime.now(timezone.utc)).astimezone(SAO_PAULO)
    opening = campaign.window_start
    closing = campaign.window_end
    if opening >= closing:
        return None
    hours, minutes = map(int, opening.split(":"))
    start = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if start <= local:
        start += timedelta(days=1)
    return start


def campaign_daily_count(db: Session, campaign: VoiceCampaign, at: datetime | None = None) -> int:
    local = (at or datetime.now(timezone.utc)).astimezone(SAO_PAULO)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(timezone.utc)
    return db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.attempts > 0,
        VoiceRecipient.last_attempt_at >= start,
    )) or 0


def next_attempt_allowed(db: Session, campaign: VoiceCampaign, at: datetime | None = None) -> bool:
    now = at or datetime.now(timezone.utc)
    if not within_call_window(campaign, now):
        return False
    if campaign_daily_count(db, campaign, now) >= min(campaign.daily_limit, MAX_CALLS_PER_DAY):
        return False
    seconds = max(MIN_SECONDS_BETWEEN_CALLS, campaign.interval_seconds)
    return bool(not campaign.last_call_at or (now - aware(campaign.last_call_at)).total_seconds() >= seconds)


def update_sip_check(db: Session, account: VoiceSipAccount, code: int | None, message: str) -> None:
    account.sip_check_at = datetime.now(timezone.utc)
    account.sip_check_code = code
    account.sip_check_message = (message or "")[:200]
