"""Layout Sulnet Prospecta: tipografia, marca e CSP de fontes em todo o sistema."""

import re

from fastapi.testclient import TestClient

from app.main import app

_FONT_LINK = re.compile(
    r'href="https://fonts\.googleapis\.com/css2\?[^"]*Manrope[^"]*Space\+Grotesk[^"]*"'
)
_BRAND_LOGO = "/static/brand/sulnet-logo.png"


def _login(client: TestClient) -> None:
    page = client.get("/login")
    token = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _csp_tokens(csp: str) -> set[str]:
    tokens: set[str] = set()
    for directive in csp.split(";"):
        tokens.update(part.strip() for part in directive.split() if part.strip())
    return tokens


def test_app_shell_uses_brand_logo_and_display_fonts():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert _BRAND_LOGO in page.text
        assert _FONT_LINK.search(page.text)
        csp_tokens = _csp_tokens(page.headers.get("content-security-policy", ""))
        assert "https://fonts.googleapis.com" in csp_tokens
        assert "https://fonts.gstatic.com" in csp_tokens


def test_conversations_and_mvp_share_sulnet_branding():
    with TestClient(app) as client:
        _login(client)
        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert _BRAND_LOGO in inbox.text
        assert _FONT_LINK.search(inbox.text)

        mvp = client.get("/conversas")
        assert mvp.status_code == 200
        assert "Prospecta Conversas" in mvp.text
        assert _BRAND_LOGO in mvp.text
        assert _FONT_LINK.search(mvp.text)
