"""Regras puras e validacoes do Prospecta VOZ (sem enviar chamadas)."""
from __future__ import annotations

import re
import wave
from io import BytesIO
from typing import BinaryIO

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


def validate_wav_file(fileobj: BinaryIO) -> None:
    """Validate WAV in bounded-memory chunks, without a business size/duration cap.

    Large recordings stay in the upload's spooled file instead of being read
    fully into RAM. The file is rewound for S3 multipart upload afterward.
    """
    try:
        fileobj.seek(0, 2)
        if fileobj.tell() < 44:
            raise ValueError("Arquivo WAV vazio ou incompleto.")
        fileobj.seek(0)
        with wave.open(fileobj, "rb") as player:
            if player.getcomptype() != "NONE" or player.getnchannels() != 1:
                raise ValueError("Use WAV PCM mono, sem compressao.")
            if player.getsampwidth() != 2 or player.getframerate() not in {8000, 16000}:
                raise ValueError("Use WAV PCM 16-bit, mono, 8 ou 16 kHz.")
            total_frames = player.getnframes()
            if total_frames <= 0:
                raise ValueError("O audio deve conter gravacao valida.")
            # Do not allocate frames for a whole recording. Reject truncated
            # WAV data rather than uploading it and failing at playback time.
            remaining = total_frames
            while remaining:
                frames = player.readframes(min(8192, remaining))
                received = len(frames) // 2  # PCM16 mono: two bytes per frame
                if received <= 0 or len(frames) % 2:
                    raise ValueError("Arquivo WAV incompleto ou truncado.")
                remaining -= received
    except (EOFError, wave.Error, OSError) as exc:
        raise ValueError("Arquivo WAV invalido.") from exc
    finally:
        fileobj.seek(0)


def validate_wav(data: bytes) -> None:
    """Backward-compatible validator for existing callers and small files."""
    with BytesIO(data) as fileobj:
        validate_wav_file(fileobj)


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
