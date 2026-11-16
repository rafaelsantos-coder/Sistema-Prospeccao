from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import ZApiInstance
from app.services.secret_store import decrypt_secret
from app.services.zapi import ZApiError, ZApiService

from test_zapi_delete import _fake_zapi, _login, _new_instance


def _new_payload(suffix: str) -> dict[str, str]:
    return {
        "name": f"Natine {suffix[:8]}",
        "base_url": "https://api.z-api.io",
        "instance_id": f"natine-unique-{suffix}",
        "instance_token": f"instance-token-{suffix}",
        "client_token": f"client-token-{suffix}",
    }


def test_register_new_natine_as_third_distinct_instance(monkeypatch):
    _fake_zapi(monkeypatch)
    monkeypatch.setattr(
        "app.routers.zapi.instance_webhook_url",
        lambda instance: "https://sulnet.example/webhooks/zapi/test",
    )
    suffix = uuid4().hex
    with TestClient(app) as client:
        token = _login(client)
        payload = {"csrf_token": token, **_new_payload(suffix)}
        response = client.post(
            "/settings/zapi/instances", data=payload, follow_redirects=False
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            created = db.scalar(
                select(ZApiInstance).where(ZApiInstance.instance_id == payload["instance_id"])
            )
            assert created is not None
            assert created.active is True
            assert created.name == payload["name"]
            assert decrypt_secret(created.instance_token_encrypted) == payload["instance_token"]
            assert decrypt_secret(created.client_token_encrypted) == payload["client_token"]


def test_register_natine_still_saves_when_webhook_fails(monkeypatch):
    _fake_zapi(monkeypatch)
    monkeypatch.setattr(
        "app.routers.zapi.instance_webhook_url",
        lambda instance: "https://sulnet.example/webhooks/zapi/test",
    )

    def webhook_unavailable(self, url):
        raise ZApiError("Client-Token not allowed")

    monkeypatch.setattr(ZApiService, "configure_webhooks", webhook_unavailable)
    suffix = uuid4().hex
    with TestClient(app) as client:
        token = _login(client)
        payload = {"csrf_token": token, **_new_payload(suffix)}
        response = client.post(
            "/settings/zapi/instances", data=payload, follow_redirects=False
        )
        assert response.status_code == 303
        page = client.get("/settings/whatsapp")
        assert "webhook não foi configurado" in page.text
        assert "Client-Token" in page.text
        with SessionLocal() as db:
            created = db.scalar(
                select(ZApiInstance).where(ZApiInstance.instance_id == payload["instance_id"])
            )
            assert created is not None and created.active


def test_register_rejects_bad_client_token_without_saving(monkeypatch):
    _fake_zapi(monkeypatch)

    def status_unavailable(self):
        raise ZApiError("Client-Token not allowed")

    monkeypatch.setattr(ZApiService, "status", status_unavailable)
    suffix = uuid4().hex
    with TestClient(app) as client:
        token = _login(client)
        payload = {"csrf_token": token, **_new_payload(suffix)}
        response = client.post(
            "/settings/zapi/instances", data=payload, follow_redirects=False
        )
        assert response.status_code == 303
        page = client.get("/settings/whatsapp")
        assert "rejeitou o Client-Token" in page.text
        with SessionLocal() as db:
            assert db.scalar(
                select(ZApiInstance).where(ZApiInstance.instance_id == payload["instance_id"])
            ) is None


def test_update_instance_name_and_credentials_without_deleting(monkeypatch):
    _fake_zapi(monkeypatch)
    monkeypatch.setattr(
        "app.routers.zapi.instance_webhook_url",
        lambda instance: "https://sulnet.example/webhooks/zapi/test",
    )
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            item = _new_instance(db)
            instance_id = item.id
            provider_id = item.instance_id
        page = client.get(f"/settings/zapi/instances/{instance_id}/edit")
        assert page.status_code == 200
        assert "Editar instância WhatsApp" in page.text
        assert "Client-Token" in page.text
        assert "token-" not in page.text
        response = client.post(
            f"/settings/zapi/instances/{instance_id}/edit",
            data={
                "csrf_token": token,
                "name": "Natine",
                "instance_token": "new-valid-instance-token",
                "client_token_mode": "replace",
                "client_token": "new-valid-client-token",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            item = db.get(ZApiInstance, instance_id)
            assert item is not None and item.active
            assert item.name == "Natine"
            assert item.instance_id == provider_id
            assert decrypt_secret(item.instance_token_encrypted) == "new-valid-instance-token"
            assert decrypt_secret(item.client_token_encrypted) == "new-valid-client-token"


def test_edit_invalid_credentials_rolls_back_and_keeps_instance(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            item = _new_instance(db)
            instance_id = item.id
            initial_name = item.name
            initial_token = decrypt_secret(item.instance_token_encrypted)

        def status_unavailable(self):
            raise ZApiError("Client-Token not allowed")

        monkeypatch.setattr(ZApiService, "status", status_unavailable)
        response = client.post(
            f"/settings/zapi/instances/{instance_id}/edit",
            data={
                "csrf_token": token,
                "name": "Natine",
                "instance_token": "invalid-token",
                "client_token_mode": "replace",
                "client_token": "invalid-client-token",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            item = db.get(ZApiInstance, instance_id)
            assert item is not None and item.active
            assert item.name == initial_name
            assert decrypt_secret(item.instance_token_encrypted) == initial_token
