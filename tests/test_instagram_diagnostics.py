"""Read-only Instagram diagnostics and admin panel."""
import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from app.database import get_db
from app.models import Branch, User
from app.models_instagram import (
    instagram_webhook_events as events,
    instagram_inbox_members as members,
    instagram_inbox_threads as threads,
    instagram_inbox_messages as messages,
)
from app.routers.instagram import router


def test_diagnostics_auth_account_isolation_and_aggregate_logs(monkeypatch, caplog):
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "101")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-signing-secret")
    monkeypatch.delenv("INSTAGRAM_INBOX_ENABLED", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (Branch.__table__, User.__table__, events, members, threads, messages):
        table.create(engine)
    with Session(engine) as db:
        db.add_all([
            User(id=1, name="Admin", email="admin@example.test", password_hash="unused",
                 role="ADMIN", must_change_password=False),
            User(id=2, name="Seller", email="seller@example.test", password_hash="unused",
                 role="CONSULTOR", must_change_password=False),
        ])
        db.commit()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="local-test-session-key")
    app.include_router(router)

    @app.get("/test-login/{user_id}")
    def login(user_id: int, request: Request):
        request.session.update(user_id=user_id, session_version=1)
        return {"ok": True}

    def db_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = db_override

    def signed(client, payload):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"test-signing-secret", raw, hashlib.sha256).hexdigest()
        return client.post("/webhooks/instagram", content=raw, headers={"X-Hub-Signature-256": sig})

    with TestClient(app) as client:
        assert client.get("/instagram", follow_redirects=False).status_code == 303
        client.get("/test-login/2")
        assert client.get("/instagram").status_code == 403
        assert client.get("/settings/instagram/status").status_code == 403
        client.get("/test-login/1")
        caplog.set_level(logging.INFO, logger="uvicorn.error")
        foreign = {"object": "instagram", "entry": [
            {"id": "102", "changes": [{"field": "comments", "value": {"text": "PRIVATE_FOREIGN"}}]},
        ]}
        own = {"object": "instagram", "entry": [
            {"id": "101", "changes": [{"field": "comments", "value": {"text": "PRIVATE_OWN"}}]},
        ]}
        assert signed(client, foreign).json()["stored"] == 0
        assert signed(client, own).json()["stored"] == 1
        assert signed(client, own).json()["stored"] == 0
        assert "matching_entries=0 matched=0 stored=0" in caplog.text
        assert "matching_entries=1 matched=1 stored=1 comments=1" in caplog.text
        assert "PRIVATE_OWN" not in caplog.text
        assert "PRIVATE_FOREIGN" not in caplog.text
        assert "test-signing-secret" not in caplog.text
        response = client.get("/settings/instagram/status")
        assert response.status_code == 200
        status = response.json()
        assert status["events_stored"] == 1
        assert status["events_by_kind"] == {"messaging": 0, "changes": 1, "standby": 0}
        assert status["inbox_enabled"] is False
        assert status["inbox_threads"] == 0
        assert status["commercial_routing_enabled"] is False
        panel = client.get("/instagram")
        assert panel.status_code == 200
        assert panel.headers["cache-control"] == "no-store"
        assert "Instagram" in panel.text
        assert "Desativada para validação" in panel.text
        assert "PRIVATE_OWN" not in panel.text
        assert "test-signing-secret" not in panel.text
        assert "data-whatsapp-presence" not in panel.text
    engine.dispose()
