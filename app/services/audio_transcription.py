"""Transcrição de áudios recebidos do cliente (WhatsApp) para a IA poder responder."""

from __future__ import annotations

import logging

import httpx

from app.config import settings

LOG = logging.getLogger("sulnet_audio")

MAX_AUDIO_BYTES = 20 * 1024 * 1024
TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
TRANSCRIPTION_MODEL = "whisper-1"
MAX_TRANSCRIPT_CHARS = 2000
TRANSCRIBED_PREFIX = "[Áudio transcrito]"
UNREADABLE_BODY = "[Áudio não transcrito]"


def strip_transcription_prefix(text: str) -> str:
    """Texto do cliente sem o marcador de áudio (o Vendedor IA lê só o conteúdo)."""
    value = (text or "").strip()
    if value.startswith(TRANSCRIBED_PREFIX):
        return value[len(TRANSCRIBED_PREFIX):].strip()
    return value


def _extension_for(mime_type: str) -> str:
    if "ogg" in mime_type or "opus" in mime_type:
        return ".ogg"
    if "mp4" in mime_type or "m4a" in mime_type or "aac" in mime_type:
        return ".m4a"
    if "webm" in mime_type:
        return ".webm"
    if "mpeg" in mime_type or "mp3" in mime_type:
        return ".mp3"
    return ".ogg"


def transcribe_audio_url(url: str | None, mime_type: str | None) -> str:
    """Baixa o áudio e devolve o texto falado. Devolve "" se não for possível transcrever."""
    if not url or not settings.openai_api_key:
        return ""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            download = client.get(url)
            download.raise_for_status()
            data = download.content
            if not data or len(data) > MAX_AUDIO_BYTES:
                return ""
            clean_mime = (
                (mime_type or download.headers.get("content-type") or "audio/ogg")
                .split(";")[0]
                .strip()
                .lower()
            )
            response = client.post(
                TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": TRANSCRIPTION_MODEL, "language": "pt"},
                files={"file": (f"audio{_extension_for(clean_mime)}", data, clean_mime)},
                timeout=40.0,
            )
            response.raise_for_status()
            text = str(response.json().get("text") or "").strip()
            return text[:MAX_TRANSCRIPT_CHARS]
    except Exception as exc:  # não pode derrubar o recebimento da mensagem
        LOG.warning("audio_transcription_failed error=%s", type(exc).__name__)
        return ""
