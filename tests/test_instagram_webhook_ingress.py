import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models_instagram import instagram_webhook_events as events
from app.routers.instagram import router
from app.services.secret_store import decrypt_secret


def test_instagram_verification_and_encrypted_idempotent_intake(monkeypatch):
    account_id = "123456789"
    secret = "local-test-app-secret"
    verify_token = "local-test-verifier"
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", account_id)
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", secret)
    monkeypatch.setenv("INSTAGRAM_WEBHOOK_VERIFY_TOKEN", verify_token)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    events.create(engine)
    app = FastAPI()
    app.include_router(router)

    def db_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    client = TestClient(app)
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": verify_token,
        "hub.challenge": "98765",
    }
    assert client.get("/webhooks/instagram", params=params).text == "98765"
    assert client.get(
        "/webhooks/instagram", params={**params, "hub.verify_token": "wrong"}
    ).status_code == 403

    event = {"sender": {"id": "customer"}, "message": {"mid": "m1", "text": "Oi"}}
    payload = {
        "object": "instagram",
        "entry": [
            {"id": account_id, "messaging": [event]},
            {"id": "another-account", "messaging": [{"message": {"text": "foreign"}}]},
        ],
    }
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/instagram", content=raw).status_code == 403
    assert client.post(
        "/webhooks/instagram", content=raw, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
    ).status_code == 403
    headers = {"X-Hub-Signature-256": signature}
    first = client.post("/webhooks/instagram", content=raw, headers=headers)
    assert first.status_code == 200
    assert first.json()["stored"] == 1
    assert client.post("/webhooks/instagram", content=raw, headers=headers).json()["stored"] == 0
    with Session(engine) as db:
        row = db.execute(select(events)).mappings().one()
        assert row["account_id"] == account_id
        assert row["event_kind"] == "messaging"
        assert row["payload_encrypted"] != json.dumps(event)
        assert json.loads(decrypt_secret(row["payload_encrypted"])) == event

    monkeypatch.delenv("INSTAGRAM_APP_SECRET")
    assert client.post("/webhooks/instagram", content=raw, headers=headers).status_code == 503
    engine.dispose()
