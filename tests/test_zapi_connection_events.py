from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, User
from app.services.zapi import ZApiError, ZApiService
from app.services.zapi_instances import create_zapi_instance
from app.timeutils import utcnow

from test_zapi_instances import _login_admin


def _connected_instance(db):
    admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
    instance = create_zapi_instance(
        db,
        name="Fila Individual - Callback Status",
        phone_label="+55 (55) 93619-3138",
        base_url="https://api.z-api.io",
        instance_id=f"callback-status-test-instance-{uuid4().hex}",
        instance_token="callback-status-test-token",
        client_token="callback-status-test-client",
        created_by_id=admin.id if admin else None,
    )
    db.commit()
    db.refresh(instance)
    return instance


def _api_unavailable(self):
    raise ZApiError("Nao foi possivel consultar o status da Z-API.")


def _device_unavailable(self):
    raise ZApiError("Nao foi possivel consultar o dispositivo da Z-API.")


def test_connected_callback_updates_device_label_and_status_fallback(monkeypatch):
    monkeypatch.setattr(ZApiService, "status", _api_unavailable)
    monkeypatch.setattr(ZApiService, "device", _device_unavailable)
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            instance = _connected_instance(db)
            response = client.post(
                f"/webhooks/zapi/instance/{instance.webhook_key}",
                json={
                    "type": "ConnectedCallback",
                    "instanceId": instance.instance_id,
                    "phone": "555591534057",
                },
            )
            assert response.status_code == 200
            assert response.json()["connection_updated"] is True
            db.refresh(instance)
            assert instance.phone_label == "+55 (55) 9153-4057"

            _login_admin(client)
            page = client.get("/settings/whatsapp")
            assert page.status_code == 200
            assert "Conectada" in page.text
            assert "Último evento Z-API" in page.text
            assert "+55 (55) 9153-4057" in page.text
        finally:
            db.close()


def test_stale_callback_does_not_mask_unavailable_status(monkeypatch):
    monkeypatch.setattr(ZApiService, "status", _api_unavailable)
    monkeypatch.setattr(ZApiService, "device", _device_unavailable)
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            instance = _connected_instance(db)
            response = client.post(
                f"/webhooks/zapi/instance/{instance.webhook_key}",
                json={"type": "ConnectedCallback", "instanceId": instance.instance_id},
            )
            assert response.status_code == 200
            event = db.scalar(
                select(AuditLog)
                .where(AuditLog.action == "ZAPI_CONNECTION_EVENT")
                .where(AuditLog.entity_id == str(instance.id))
                .order_by(AuditLog.id.desc())
            )
            assert event is not None
            event.created_at = utcnow() - timedelta(minutes=20)
            db.commit()

            _login_admin(client)
            page = client.get("/settings/whatsapp")
            assert page.status_code == 200
            assert "Não confirmado" in page.text
            assert "Consulta de status indisponível" in page.text
        finally:
            db.close()


def test_disconnected_callback_updates_recent_event(monkeypatch):
    monkeypatch.setattr(ZApiService, "status", _api_unavailable)
    monkeypatch.setattr(ZApiService, "device", _device_unavailable)
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            instance = _connected_instance(db)
            response = client.post(
                f"/webhooks/zapi/instance/{instance.webhook_key}",
                json={"type": "DisconnectedCallback", "instanceId": instance.instance_id},
            )
            assert response.status_code == 200
            assert response.json()["connected"] is False
            _login_admin(client)
            page = client.get("/settings/whatsapp")
            assert page.status_code == 200
            assert "Desconectada" in page.text
            assert "Último evento Z-API" in page.text
        finally:
            db.close()
