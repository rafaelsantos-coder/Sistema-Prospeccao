"""Regressoes do audio capturado pelo MediaRecorder no Chrome."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services.media_processing import normalize_audio
from app.services.upload_security import UploadValidationError, validate_upload
from app.routers.conversations import _media_kind


WEBM_HEADER = b"\x1a\x45\xdf\xa3"


@pytest.mark.parametrize("mime", ["audio/webm", "audio/webm;codecs=opus"])
def test_chrome_mediarecorder_webm_is_accepted_as_audio(mime):
    assert validate_upload(
        filename="audio-123.webm", mime_type=mime, data=WEBM_HEADER + b"x" * 32
    ) == ".webm"
    assert _media_kind("audio-123.webm", mime) == "AUDIO"


@pytest.mark.parametrize(
    ("mime", "contents"),
    [
        ("video/webm", WEBM_HEADER + b"x" * 32),
        ("application/octet-stream", WEBM_HEADER + b"x" * 32),
        ("audio/webm", b"not-an-ebml-container"),
    ],
)
def test_rejects_untrusted_or_invalid_webm(mime, contents):
    with pytest.raises(UploadValidationError):
        validate_upload(filename="audio.webm", mime_type=mime, data=contents)


def test_webm_recording_is_converted_to_whatsapp_ogg():
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg indisponivel no ambiente de teste")
    recorded = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-t", "0.25", "-c:a", "libopus", "-f", "webm", "pipe:1",
        ],
        check=True, capture_output=True, timeout=20,
    ).stdout
    assert recorded.startswith(WEBM_HEADER)
    validate_upload(filename="audio.webm", mime_type="audio/webm", data=recorded)
    audio, name, mime = normalize_audio(
        recorded, filename="audio.webm", mime_type="audio/webm"
    )
    assert audio.startswith(b"OggS")
    assert name == "audio.ogg"
    assert mime == "audio/ogg"
