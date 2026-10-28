from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app
from app.models import Conversation, User
from app.services.conversation_engine import conversation_visible_to_user
from app.services.media_storage import MediaStorage
from app.services.zapi import ZApiService


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
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


def test_open_conversation_from_lead():
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        response = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/conversations/")
        inbox = client.get(response.headers["location"])
        assert inbox.status_code == 200
        assert "Caixa de entrada" in inbox.text
        assert "conversation-page-content" in inbox.text
        assert "Clínica Horizonte" in inbox.text


def test_send_message_from_inbox(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "TEST-INBOX-MESSAGE-1",
            "zaapId": "TEST-ZAAP-1",
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/send",
            data={
                "csrf_token": token,
                "message": "Mensagem automatizada de teste do Inbox.",
            },
            follow_redirects=False,
        )
        assert sent.status_code == 303
        page = client.get(location)
        assert "Mensagem automatizada de teste do Inbox." in page.text


def test_inbox_requires_login():
    with TestClient(app) as client:
        response = client.get("/conversations", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_consultant_cannot_see_another_consultants_conversation():
    owner = User(id=101, name="Consultor A", email="a@example.com", password_hash="x", role="CONSULTOR", team="B2B")
    other = User(id=102, name="Consultor B", email="b@example.com", password_hash="x", role="CONSULTOR", team="B2B")
    admin = User(id=1, name="Admin", email="admin@example.com", password_hash="x", role="ADMIN", team="LOJA")
    conversation = Conversation(id=900, contact_id=1, assigned_user_id=owner.id, team="B2B", status="EM_ATENDIMENTO")
    assert conversation_visible_to_user(conversation, owner) is True
    assert conversation_visible_to_user(conversation, other) is False
    assert conversation_visible_to_user(conversation, admin) is True


def test_realtime_inbox_api_lists_new_conversation():
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        state = client.get("/api/conversations/inbox?view=active")
        assert state.status_code == 200
        payload = state.json()
        assert payload["counts"]["active"] >= 1
        assert any(item["name"] == "Clínica Horizonte" for item in payload["items"])


def test_send_image_from_inbox(monkeypatch):
    monkeypatch.setattr(
        MediaStorage,
        "upload_bytes",
        lambda self, **kwargs: ("s3://conversationmedia/test/foto.jpg", "foto.jpg"),
    )
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/foto.jpg",
    )
    monkeypatch.setattr(
        ZApiService,
        "send_image",
        lambda self, *, phone, image_url, caption="": {
            "messageId": "TEST-IMAGE-1",
            "zaapId": "TEST-ZAAP-IMAGE-1",
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/media",
            data={"csrf_token": token, "caption": "Foto de teste"},
            files={"media": ("foto.jpg", b"fake-jpeg", "image/jpeg")},
        )
        assert sent.status_code == 200
        assert sent.json()["ok"] is True
        page = client.get(location)
        assert "Foto de teste" in page.text
        assert "message-media-preview" in page.text


def test_send_audio_from_inbox(monkeypatch):
    monkeypatch.setattr(
        MediaStorage,
        "upload_bytes",
        lambda self, **kwargs: ("s3://conversationmedia/test/audio.ogg", "audio.ogg"),
    )
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/audio.ogg",
    )
    monkeypatch.setattr(
        ZApiService,
        "send_audio",
        lambda self, *, phone, audio_url: {
            "messageId": "TEST-AUDIO-1",
            "zaapId": "TEST-ZAAP-AUDIO-1",
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/media",
            data={"csrf_token": token, "caption": ""},
            files={"media": ("audio.ogg", b"fake-ogg", "audio/ogg")},
        )
        assert sent.status_code == 200
        assert sent.json()["type"] == "AUDIO"
        page = client.get(location)
        assert "message-audio-wrap" in page.text
