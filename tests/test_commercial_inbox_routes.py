"""Teste real das telas e do POST interno de etiquetas."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client):
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local",
        "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303


def test_manager_can_open_commercial_dashboard_and_tag_conversation():
    with TestClient(app) as client:
        _login(client)
        board = client.get("/conversations-intelligence")
        assert board.status_code == 200
        assert "Mapa de calor comercial" in board.text
        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert 'name="tag"' in inbox.text
        assert 'name="temperature"' in inbox.text
        assert 'data-inbox-filter-drawer' in inbox.text
        assert 'id="inbox-queue-select"' in inbox.text
        assert 'class="inbox-filter-popup"' in inbox.text
        assert 'class="inbox-shortcut-group"' in inbox.text
        assert 'aria-label="Situação dos atendimentos"' in inbox.text
        assert "Filtros" in inbox.text
        lead_page = client.get("/leads/1")
        opened = client.post("/leads/1/conversation", data={
            "csrf_token": _csrf(lead_page.text),
        }, follow_redirects=False)
        assert opened.status_code == 303
        path = opened.headers["location"]
        conversation_id = int(path.rstrip("/").split("/")[-1])
        detail = client.get(path)
        assert detail.status_code == 200
        assert "Adicionar etiqueta" in detail.text
        assert 'class="commercial-tag-shortcuts"' in detail.text
        assert 'class="commercial-tag-picker"' in detail.text
        assert 'data-tag="EM_NEGOCIACAO"' in detail.text
        assert 'data-tag="ALTO_POTENCIAL"' in detail.text
        assert '<optgroup label="Documentos e contrato">' in detail.text
        saved = client.post(f"/conversations/{conversation_id}/commercial-tag", data={
            "csrf_token": _csrf(detail.text),
            "code": "ALTO_POTENCIAL",
            "enabled": "1",
        }, follow_redirects=False)
        assert saved.status_code == 303
        filtered = client.get("/api/conversations/inbox?tag=ALTO_POTENCIAL")
        assert filtered.status_code == 200
        assert any(item["id"] == conversation_id for item in filtered.json()["items"])
        # Etiquetas persistidas e recebidas pelo polling usam a mesma paleta.
        detail_with_tag = client.get(path)
        assert detail_with_tag.status_code == 200
        assert 'data-tag="ALTO_POTENCIAL"' in detail_with_tag.text
        assert '.commercial-tag[data-tag="ALTO_POTENCIAL"]' in detail_with_tag.text
        assert '.commercial-tag[data-tag="EM_NEGOCIACAO"]' in detail_with_tag.text
        assert '.commercial-tag[data-tag="DOCUMENTOS_PENDENTES"]' in detail_with_tag.text
        assert '.commercial-tag[data-tag="RISCO_PERDA"]' in detail_with_tag.text
        assert "badge.dataset.tag = t.code || ''" in detail_with_tag.text
        board_with_tag = client.get("/conversations-intelligence")
        assert board_with_tag.status_code == 200
        assert 'class="commercial-tag" data-tag="ALTO_POTENCIAL"' in board_with_tag.text
        rejected = client.post(f"/conversations/{conversation_id}/commercial-tag", data={
            "csrf_token": _csrf(client.get(path).text),
            "code": "INVALIDO",
            "enabled": "1",
        }, follow_redirects=False)
        assert rejected.status_code == 400
