"""O chat precisa poder gravar áudio no próprio domínio."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.security_middleware import SecurityHeadersMiddleware


def test_permissions_policy_allows_same_origin_microphone():
    source = Path("app/security_middleware.py").read_text(encoding="utf-8")
    assert '"camera=(), microphone=(self), geolocation=(), payment=()"' in source
    assert "microphone=(self)" in source


def test_inbox_treats_security_error_as_permission_block():
    html = Path("app/templates/conversations/inbox.html").read_text(encoding="utf-8")
    assert "error?.name === 'SecurityError'" in html
    assert "Permissions-Policy" in Path("app/security_middleware.py").read_text(
        encoding="utf-8"
    )


def test_microphone_policy_overrides_downstream_deny_header():
    """Uma outra rota nao deve sobrescrever a permissao do microfone."""
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/mic-headers-test")
    def route():
        return PlainTextResponse("ok", headers={"Permissions-Policy": "microphone=()"})

    with TestClient(app) as client:
        response = client.get("/mic-headers-test")
    policy = response.headers["Permissions-Policy"]
    assert "microphone=(self)" in policy
    assert "camera=()" in policy
    assert "geolocation=()" in policy
    assert "payment=()" in policy
    assert "microphone=()" not in policy
