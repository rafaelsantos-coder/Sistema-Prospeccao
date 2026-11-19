"""Regressões de controles que não dependem de serviços externos."""
import logging

from app.main import _RedactWebhookSecretFilter
from app.services.commercial_intelligence import run_commercial_observer_loop


def _sanitize(path: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s "%s %s" %s',
        ("127.0.0.1", "GET", path, "HTTP/1.1"), None,
    )
    assert _RedactWebhookSecretFilter().filter(record)
    return record.args[2]


def test_diagnostic_secret_is_redacted_from_access_log():
    result = _sanitize("/internal/diag/example-not-a-real-secret")
    assert result == "/internal/diag/***"
    assert "example-not-a-real-secret" not in result


def test_webhook_secret_is_redacted_from_access_log():
    assert _sanitize("/webhooks/zapi/secret-value") == "/webhooks/zapi/***"
    assert _sanitize("/webhooks/zapi/instance/secret-value") == "/webhooks/zapi/instance/***"


def test_unrelated_path_is_unchanged():
    assert _sanitize("/api/health") == "/api/health"


def test_commercial_observer_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COMMERCIAL_OBSERVER_ENABLED", raising=False)
    run_commercial_observer_loop()

def test_diagnostic_header_uses_same_secret_validation(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.routers import diagnostico as diagnostic_router

    monkeypatch.setattr(
        diagnostic_router, "settings",
        SimpleNamespace(diagnostics_secret="x" * 32),
    )
    try:
        diagnostic_router.diagnostico_via_header("incorrect-key", None)
        assert False, "invalid diagnostic header must be denied"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_diagnostic_path_secret_redaction_preserves_query():
    sanitized = _sanitize("/internal/diag/secret-value?check=1")
    assert sanitized == "/internal/diag/***?check=1"
