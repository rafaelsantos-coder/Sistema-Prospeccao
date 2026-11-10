"""Download do áudio só de hosts de mídia permitidos (CodeQL py/full-ssrf)."""

from __future__ import annotations

from app.services import audio_transcription as audio


def test_media_url_only_from_allowed_hosts(monkeypatch):
    ok = "https://f004.backblazeb2.com/file/temp-file-download/instances/X/a.ogg?x=1"
    assert audio.safe_media_url(ok) == ok
    assert audio.safe_media_url("http://f004.backblazeb2.com/a.ogg") == ""
    assert audio.safe_media_url("https://169.254.169.254/latest/meta-data") == ""
    assert audio.safe_media_url("https://evil.example/a.ogg") == ""
    monkeypatch.setenv("AUDIO_MEDIA_HOSTS", "midia.exemplo.com")
    assert audio.safe_media_url("https://midia.exemplo.com/a.ogg") == "https://midia.exemplo.com/a.ogg"


def test_prefix_is_removed_for_the_seller():
    assert audio.strip_transcription_prefix("[Áudio transcrito] João da Silva") == "João da Silva"
    assert audio.strip_transcription_prefix("Oi") == "Oi"
