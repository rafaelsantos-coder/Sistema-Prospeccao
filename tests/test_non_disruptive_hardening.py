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
