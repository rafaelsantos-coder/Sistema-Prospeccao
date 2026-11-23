"""Cobertura do Prospecta Conversas (MVP web de chat).

Nao usa CRM, instancia paralela ou historico por telefone: os endpoints
permanecem os do Prospecta, com verificacao de permissao por requisicao.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.access_control import permission_for_request
from app.services.zapi import ZApiService


def _csrf(markup: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', markup)
    assert match is not None
    return match.group(1)


def _login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": _csrf(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_conversas_mvp_reuses_login_and_has_no_crm_navigation():
    with TestClient(app) as client:
        anon = client.get("/conversas", follow_redirects=False)
        assert anon.status_code == 303
        assert anon.headers["location"] == "/login"
        _login(client)
        page = client.get("/conversas")
        assert page.status_code == 200
        assert 'id="mvp-app"' in page.text
        assert "Prospecta Conversas" in page.text
        assert 'id="mvp-conversation-list"' in page.text
        assert 'id="mvp-messages"' in page.text
        assert "/static/js/conversas_mvp.js" in page.text
        assert "/static/css/conversas_mvp.css" in page.text
        assert "Registrar CRM" not in page.text
        assert "campanhas" not in page.text.lower()
        assert "Cache-Control" in page.headers
        assert "no-store" in page.headers["Cache-Control"]
        deep_link = client.get("/conversas/98765432")
        assert deep_link.status_code == 200
        assert 'data-initial-id="98765432"' in deep_link.text
        # A pagina de entrada nunca embute o conteudo de uma conversa.
        assert "mensagem privada" not in deep_link.text


def test_conversas_mvp_routes_require_existing_module_permissions():
    assert permission_for_request("/conversas", "GET") == "conversations.view"
    assert permission_for_request("/conversas/22", "GET") == "conversations.view"
    assert permission_for_request("/conversas/22", "POST") == "conversations.reply"
    assert permission_for_request("/api/conversations/22/messages", "GET") == "conversations.view"
    assert permission_for_request("/conversations/22/send", "POST") == "conversations.reply"


def test_conversas_mvp_json_text_send_reuses_secure_inbox(monkeypatch):
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {"messageId": "MVP-TEST-SEND-1"},
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": _csrf(lead.text)},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        target = opened.headers["location"]
        page = client.get("/conversas")
        response = client.post(
            target + "/send",
            data={"csrf_token": _csrf(page.text), "message": "Ola pelo MVP"},
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["status"] == "SENT"
        assert isinstance(response.json()["message_id"], int)
        assert client.get(target + "/messages").status_code in {200, 404}


def test_conversas_mvp_json_send_blocks_contact(monkeypatch):
    monkeypatch.setattr("app.routers.conversations.is_globally_blocked", lambda *_: True)
    def must_not_send(*_args, **_kwargs):
        raise AssertionError("Nao pode enviar contato bloqueado")
    monkeypatch.setattr(ZApiService, "send_text", must_not_send)
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": _csrf(lead.text)},
            follow_redirects=False,
        )
        target = opened.headers["location"]
        page = client.get("/conversas")
        response = client.post(
            target + "/send",
            data={"csrf_token": _csrf(page.text), "message": "Nao enviar"},
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.json()["ok"] is False


def test_conversas_mvp_javascript_parses():
    if not shutil.which("node"):
        return
    source = Path(__file__).parents[1] / "app/static/js/conversas_mvp.js"
    result = subprocess.run(
        ["node", "--check", str(source)], capture_output=True, text=True, check=False, timeout=15
    )
    assert result.returncode == 0, result.stderr
