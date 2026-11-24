"""Cobertura do Prospecta Conversas (MVP web de chat).

Nao usa CRM, instancia paralela ou historico por telefone: os endpoints
permanecem os do Prospecta, com verificacao de permissao por requisicao.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from uuid import uuid4
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import Conversation, ConversationQueueLink, ConversationZApiInstance, Message, ServiceQueue
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance
from sqlalchemy import func, select
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
        conversation_id = target.rsplit("/", 1)[-1]
        messages = client.get(f"/api/conversations/{conversation_id}/messages")
        assert messages.status_code == 200
        assert any(item["body"] == "Ola pelo MVP" for item in messages.json()["items"])


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


def _create_outbound_test_queue() -> int:
    suffix = uuid4().hex[:12]
    with SessionLocal() as db:
        queue = ServiceQueue(
            name="Fila do MVP - Teste",
            code="MVP-QUICK-" + suffix,
            team="LOJA",
            routing_strategy="MANUAL",
            active=True,
        )
        db.add(queue)
        db.flush()
        instance = create_zapi_instance(
            db, name="Z-API MVP Teste", phone_label="Teste",
            base_url="https://api.z-api.io",
            instance_id="test-mvp-quick-" + suffix,
            instance_token="test-mvp-quick-token",
            client_token="test-mvp-quick-client-token",
            created_by_id=None,
        )
        bind_queue_instance(db, queue.id, instance.id)
        queue_id = queue.id
        db.commit()
        return queue_id


def test_conversas_mvp_new_chat_json_creates_owner_without_sending():
    with TestClient(app) as client:
        _login(client)
        queue_id = _create_outbound_test_queue()
        page = client.get("/conversas")
        assert page.status_code == 200
        assert 'id="mvp-new-open"' in page.text
        assert 'id="mvp-new-dialog"' in page.text
        assert f'<option value="{queue_id}">' in page.text
        token = _csrf(page.text)
        response = client.post(
            "/conversations/start",
            data={"csrf_token": token, "phone": "(55) 99991-2037", "queue_id": queue_id},
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["reused"] is False
        conversation_id = payload["conversation_id"]
        assert payload["owner"]
        assert payload["queue_name"] == "Fila do MVP - Teste"
        with SessionLocal() as db:
            conv = db.get(Conversation, conversation_id)
            assert conv and conv.status == "ATRIBUIDO"
            assert conv.assigned_user_id is not None
            link = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == conversation_id
            ))
            assert link and link.queue_id == queue_id
            instance = db.scalar(select(ConversationZApiInstance).where(
                ConversationZApiInstance.conversation_id == conversation_id
            ))
            assert instance is not None
            assert db.scalar(select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id
            )) == 0
        inbox = client.get("/api/conversations/inbox?view=active").json()["items"]
        assert any(x["id"] == conversation_id and x["owner"] == payload["owner"] for x in inbox)
        again = client.post(
            "/conversations/start",
            data={"csrf_token": token, "phone": "5555999912037", "queue_id": queue_id},
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert again.status_code == 200
        assert again.json()["conversation_id"] == conversation_id
        assert again.json()["reused"] is True


def test_conversas_mvp_new_chat_json_rejects_invalid_queue_and_phone():
    with TestClient(app) as client:
        _login(client)
        queue_id = _create_outbound_test_queue()
        token = _csrf(client.get("/conversas").text)
        missing_queue = client.post(
            "/conversations/start",
            data={"csrf_token": token, "phone": "(55) 99991-2055", "queue_id": 999999},
            headers={"Accept": "application/json"}, follow_redirects=False,
        )
        assert missing_queue.status_code == 403
        assert missing_queue.json()["ok"] is False
        invalid_phone = client.post(
            "/conversations/start",
            data={"csrf_token": token, "phone": "123", "queue_id": queue_id},
            headers={"Accept": "application/json"}, follow_redirects=False,
        )
        assert invalid_phone.status_code == 400
        assert invalid_phone.json()["ok"] is False


def test_conversas_mvp_manager_owner_visible_only_with_management_role():
    source = (Path(__file__).parents[1] / "app/static/js/conversas_mvp.js").read_text(
        encoding="utf-8"
    )
    assert '["ADMIN", "GESTOR", "SUPERVISOR"].includes(root.dataset.role)' in source
    assert '"Responsável: "' in source
    assert 'ui.meta.textContent = details.filter(Boolean).join(" · ")' in source
    assert 'info.append(responsible)' in source
