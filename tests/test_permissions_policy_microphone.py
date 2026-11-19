"""O chat precisa poder gravar áudio no próprio domínio."""

from __future__ import annotations

from pathlib import Path


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
