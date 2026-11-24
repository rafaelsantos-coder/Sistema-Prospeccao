"""Layout Sulnet Prospecta: tipografia, marca e CSP de fontes em todo o sistema."""

from fastapi.testclient import TestClient

from app.main import app


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


def test_app_shell_uses_brand_logo_and_display_fonts():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "/static/brand/sulnet-logo.png" in page.text
        assert "fonts.googleapis.com" in page.text
        assert "Manrope" in page.text
        assert "Space+Grotesk" in page.text
        csp = page.headers.get("content-security-policy", "")
        assert "https://fonts.googleapis.com" in csp
        assert "https://fonts.gstatic.com" in csp


def test_conversations_and_mvp_share_sulnet_branding():
    with TestClient(app) as client:
        _login(client)
        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert "/static/brand/sulnet-logo.png" in inbox.text
        assert "Manrope" in inbox.text

        mvp = client.get("/conversas")
        assert mvp.status_code == 200
        assert "Prospecta Conversas" in mvp.text
        assert "/static/brand/sulnet-logo.png" in mvp.text
        assert "Manrope" in mvp.text
        assert "Space+Grotesk" in mvp.text
