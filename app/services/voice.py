"""Regras puras e validacoes do Prospecta VOZ (sem enviar chamadas)."""
from __future__ import annotations

import re
import wave
from io import BytesIO

from app.services.scoring import normalize_phone

MODES = {"AUDIO", "DIRECT"}
SIP_TRANSPORTS = {"UDP", "TCP", "TLS"}

# voice_campaigns.status is VARCHAR(20) in production. Keep state keys within this limit.
VOICE_STATUS_HOMOLOGACAO = "EM_HOMOLOGACAO"
_EXT = re.compile(r"^[0-9*#]{1,20}$")
_HOST = re.compile(r"^(?=.{1,190}$)[A-Za-z0-9][A-Za-z0-9.-]*$")
_HHMM = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


def validate_sip(domain: str, username: str, transport: str) -> tuple[str, str, str]:
    host = domain.strip().lower()
    user = username.strip()
    proto = transport.strip().upper()
    if not _HOST.fullmatch(host) or ".." in host or host.endswith("."):
        raise ValueError("Informe um dominio SIP valido, sem protocolo ou endereco IP de destino arbitrario.")
    if not _EXT.fullmatch(user):
        raise ValueError("O usuario SIP deve conter apenas digitos, * ou #.")
    if proto not in SIP_TRANSPORTS:
        raise ValueError("Transporte SIP invalido.")
    return host, user, proto


def validate_extension(value: str) -> str:
    clean = value.strip()
    if not _EXT.fullmatch(clean):
        raise ValueError("Ramal invalido: use somente numeros, * ou # (maximo 20 caracteres).")
    return clean


def validate_campaign(mode: str, start: str, end: str, digit: str, audio_uri: str) -> str:
    clean = mode.strip().upper()
    if clean not in MODES:
        raise ValueError("Selecione AUDIO ou DIRECT.")
    if not _HHMM.fullmatch(start) or not _HHMM.fullmatch(end) or start >= end:
        raise ValueError("Informe uma janela valida com horario inicial anterior ao final.")
    if len(digit) != 1 or digit not in "0123456789*#":
        raise ValueError("Informe somente uma tecla DTMF.")
    if clean == "AUDIO" and not audio_uri:
        raise ValueError("A campanha com audio exige arquivo WAV valido.")
    return clean


def validate_wav(data: bytes, max_bytes: int = 2_000_000) -> None:
    if not data or len(data) > max_bytes:
        raise ValueError("Audio WAV vazio ou maior que 2 MB.")
    try:
        with wave.open(BytesIO(data), "rb") as player:
            if player.getcomptype() != "NONE" or player.getnchannels() != 1:
                raise ValueError("Use WAV PCM mono, sem compressao.")
            if player.getsampwidth() != 2 or player.getframerate() not in {8000, 16000}:
                raise ValueError("Use WAV PCM 16-bit, mono, 8 ou 16 kHz.")
            duration = player.getnframes() / player.getframerate()
            if not 0.5 <= duration <= 60:
                raise ValueError("O audio deve durar de 0,5 a 60 segundos.")
    except (EOFError, wave.Error) as exc:
        raise ValueError("Arquivo WAV invalido.") from exc


def normalize_voice_contacts(rows: list[dict[str, str]], blocked: set[str]) -> tuple[list[dict[str, str]], int]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    skipped = 0
    for row in rows:
        phone = normalize_phone(row.get("phone"))
        if not phone or phone in seen or phone in blocked:
            skipped += 1
            continue
        seen.add(phone)
        result.append({"phone": phone, "name": (row.get("name") or phone)[:150]})
    return result, skipped
