"""Teste de ponta a ponta como cliente, sem WhatsApp Web (05/11)."""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.routers import ai_agents
from app.routers import zapi as zapi_router
from app.services import ai_sdr


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        follow_redirects=False,
    )
    page = client.get("/ai/simulator")
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    return match.group(1)


def test_mensagem_de_teste_entra_pelo_mesmo_caminho_do_webhook(monkeypatch):
    captured = {}

    def _fake_process(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"ok": True, "conversation_id": 777, "message_id": 1, "created": True}

    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, ai_test_phones=("5555999002508",)))
    monkeypatch.setattr(ai_agents, "queue_instance", lambda db, queue_id: SimpleNamespace(id=3, instance_id="INST"))
    monkeypatch.setattr(zapi_router, "_process_zapi_payload", _fake_process)
    with TestClient(app) as client:
        csrf = _login(client)
        response = client.post(
            "/ai/test-chat/inbound",
            json={"csrf_token": csrf, "phone": "55 99900-2508", "text": "Oi", "queue_id": 2},
        )
        assert response.status_code == 200, response.text
        assert response.json()["conversation_id"] == 777
        payload = captured["payload"]
        assert payload["phone"] == "5599900" + "2508"
        assert payload["text"] == {"message": "Oi"} and payload["fromMe"] is False
        assert payload["messageId"].startswith("TESTE-")
        assert captured["kwargs"]["queue_id"] == 2 and captured["kwargs"]["zapi_instance_id"] == 3


def test_telefone_fora_da_lista_de_teste_e_recusado(monkeypatch):
    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, ai_test_phones=("5555999002508",)))
    monkeypatch.setattr(ai_agents, "queue_instance", lambda db, queue_id: SimpleNamespace(id=3, instance_id="INST"))
    with TestClient(app) as client:
        csrf = _login(client)
        response = client.post(
            "/ai/test-chat/inbound",
            json={"csrf_token": csrf, "phone": "5511987654321", "text": "Oi", "queue_id": 2},
        )
        assert response.status_code == 400
        assert "AI_TEST_PHONES" in response.json()["error"]
