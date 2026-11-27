"""Garante que a conexao experimental removida nao apareca nem receba eventos."""
import re

from fastapi.testclient import TestClient

from app.database import Base
from app.main import app
from app.routers.queues import WHATSAPP_PROVIDERS


def _csrf(page):
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert m is not None
    return m.group(1)


def test_removed_provider_not_in_live_schema_or_queue_options():
    assert "sulnet_wa_instances" not in Base.metadata.tables
    assert set(WHATSAPP_PROVIDERS) == {"ZAPI", "META"}


def test_removed_routes_return_404_and_settings_hide_links():
    with TestClient(app) as client:
        assert client.get("/settings/sulnet-wa", follow_redirects=False).status_code == 404
        assert client.post("/webhooks/sulnet-wa", content=b"{}").status_code == 404
        token = _csrf(client.get("/login").text)
        response = client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        for url in ("/settings/whatsapp", "/settings/queues", "/settings/integrations"):
            page = client.get(url)
            assert page.status_code == 200
            assert "Motor WhatsApp Sulnet" not in page.text
            assert "/settings/sulnet-wa" not in page.text
            assert 'value="SULNET_WA"' not in page.text
