from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    token = _csrf(page.text)
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


def test_settings_is_a_navigation_hub():
    with TestClient(app) as client:
        _login_admin(client)
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Configurações do ambiente" in page.text
        assert "Números e conexões Z-API" in page.text
        assert "Filas de atendimento" in page.text
        assert "Usuários e acessos" in page.text
        assert "Metas e unidades" in page.text
        assert "Integrações e ambiente" in page.text
        assert "Auditoria" in page.text
        assert "Criar fila de atendimento" not in page.text
        assert "Novo usuário" not in page.text


def test_settings_subpages_load_independently():
    with TestClient(app) as client:
        _login_admin(client)
        expected = {
            "/settings/users": "Usuários e acessos",
            "/settings/queues": "Filas de atendimento",
            "/settings/whatsapp": "Números e conexões Z-API",
            "/settings/operations": "Metas e unidades",
            "/settings/integrations": "Integrações e ambiente",
            "/settings/audit": "Auditoria",
        }
        for path, title in expected.items():
            page = client.get(path)
            assert page.status_code == 200, path
            assert title in page.text, path
            assert 'class="settings-section-nav"' in page.text, path
