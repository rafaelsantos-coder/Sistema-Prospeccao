from __future__ import annotations

import re
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, User
from app.services.zapi import ZApiError, ZApiService
from app.services.zapi_instances import create_zapi_instance

from test_zapi_instances import _login_admin


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _patch_delay_settings(monkeypatch, *, message: int, typing: int) -> None:
    monkeypatch.setattr(
        "app.services.zapi.settings",
        replace(
            settings,
            zapi_delay_message_seconds=message,
            zapi_delay_typing_seconds=typing,
        ),
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, *, response=None, post_handler=None, delete_handler=None):
        self._response = response or _FakeResponse()
        self._post_handler = post_handler
        self._delete_handler = delete_handler
        self.posts: list[dict] = []
        self.deletes: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers or {}, "json": json})
        if self._post_handler:
            return self._post_handler(url, headers, json)
        return self._response

    def delete(self, url, headers=None):
        self.deletes.append({"url": url, "headers": headers or {}})
        if self._delete_handler:
            return self._delete_handler(url, headers)
        return self._response

    def get(self, url, headers=None):
        return self._response


def _service() -> ZApiService:
    return ZApiService(
        base_url="https://api.z-api.io",
        instance_id="inst-test",
        instance_token="token-test",
        client_token="client-test",
    )


def _allow_outbound(monkeypatch) -> None:
    monkeypatch.setattr(
        ZApiService,
        "_assert_recipient_not_blacklisted",
        staticmethod(lambda phone: None),
    )


def test_send_text_includes_configured_delays(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(payload={"messageId": "abc"}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    _allow_outbound(monkeypatch)
    _patch_delay_settings(monkeypatch, message=2, typing=3)

    result = _service().send_text(phone="5555999999999", message="Olá")
    assert result["messageId"] == "abc"
    assert len(fake.posts) == 1
    body = fake.posts[0]["json"]
    assert body["delayMessage"] == 2
    assert body["delayTyping"] == 3
    assert body["phone"] == "5555999999999"


def test_send_text_omits_delays_when_zero(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(payload={"messageId": "abc"}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    _allow_outbound(monkeypatch)
    _patch_delay_settings(monkeypatch, message=0, typing=0)

    _service().send_text(phone="5555999999999", message="Olá")
    body = fake.posts[0]["json"]
    assert "delayMessage" not in body
    assert "delayTyping" not in body


def test_send_text_explicit_delay_overrides_settings(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(payload={"messageId": "abc"}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    _allow_outbound(monkeypatch)
    _patch_delay_settings(monkeypatch, message=2, typing=3)

    _service().send_text(
        phone="5555999999999",
        message="Olá",
        delay_message=5,
        delay_typing=7,
    )
    body = fake.posts[0]["json"]
    assert body["delayMessage"] == 5
    assert body["delayTyping"] == 7


def test_clamp_delay_rejects_above_15():
    with pytest.raises(ZApiError):
        ZApiService._clamp_delay(16)


def test_list_queue_parses_page(monkeypatch):
    fake = _FakeClient(
        response=_FakeResponse(
            payload={
                "messages": [
                    {
                        "ZaapId": "z1",
                        "Phone": "5511999999999",
                        "CreatedAt": "2026-11-26T00:00:00+00:00",
                    }
                ],
                "pagingState": "cursor-1",
                "hasMore": True,
            }
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    page = _service().list_queue(page_size=20)
    assert page["count"] == 1
    assert page["has_more"] is True
    assert page["paging_state"] == "cursor-1"
    assert fake.posts[0]["url"].endswith("/queue")
    assert fake.posts[0]["json"]["pageSize"] == 20


def test_queue_pending_summary_aggregates_pages(monkeypatch):
    pages = [
        {
            "messages": [{"ZaapId": "a", "Phone": "5511"}, {"ZaapId": "b", "Phone": "5512"}],
            "pagingState": "c1",
            "hasMore": True,
        },
        {
            "messages": [{"ZaapId": "c", "Phone": "5513"}],
            "pagingState": None,
            "hasMore": False,
        },
    ]
    state = {"i": 0}

    def _handler(url, headers, json):
        payload = pages[state["i"]]
        state["i"] += 1
        return _FakeResponse(payload=payload)

    fake = _FakeClient(post_handler=_handler)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    summary = _service().queue_pending_summary(max_pages=5)
    assert summary["count"] == 3
    assert summary["has_more"] is False
    assert summary["capped"] is False
    assert len(summary["samples"]) == 2


def test_clear_queue_calls_delete(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(payload={"value": True}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)
    result = _service().clear_queue()
    assert result.get("value") is True
    assert len(fake.deletes) == 1
    assert fake.deletes[0]["url"].endswith("/queue")


def test_connected_callback_records_pending_queue(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "queue_pending_summary",
        lambda self, *, max_pages=5: {
            "count": 4,
            "has_more": False,
            "capped": False,
            "samples": [],
        },
    )
    monkeypatch.setattr(
        "app.services.ops_alerts.send_ops_alert",
        lambda *args, **kwargs: False,
    )
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            instance = create_zapi_instance(
                db,
                name="Fila Native Callback",
                phone_label="+55 (55) 90000-0001",
                base_url="https://api.z-api.io",
                instance_id=f"native-queue-{uuid4().hex}",
                instance_token="native-queue-token",
                client_token="native-queue-client",
                created_by_id=admin.id if admin else None,
            )
            db.commit()
            db.refresh(instance)
            response = client.post(
                f"/webhooks/zapi/instance/{instance.webhook_key}",
                json={
                    "type": "ConnectedCallback",
                    "instanceId": instance.instance_id,
                    "phone": "5555900000001",
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["connection_updated"] is True
            assert body["native_queue_pending"] == 4
            event = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "ZAPI_CONNECTION_EVENT",
                    AuditLog.entity_id == str(instance.id),
                )
                .order_by(AuditLog.id.desc())
            )
            assert event is not None
            assert '"native_queue_pending": 4' in (event.payload_json or "")
        finally:
            db.close()


def test_clear_queue_route_requires_confirm_and_audits(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "queue_pending_summary",
        lambda self, *, max_pages=5: {
            "count": 2,
            "has_more": False,
            "capped": False,
            "samples": [],
        },
    )
    monkeypatch.setattr(ZApiService, "clear_queue", lambda self: {"ok": True})
    monkeypatch.setattr(
        ZApiService,
        "status",
        lambda self: {
            "configured": True,
            "connected": True,
            "smartphone_connected": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        ZApiService,
        "device",
        lambda self: {
            "phone": "5555999999999",
            "name": "Teste",
            "img_url": "",
            "about": "",
            "is_business": False,
            "original_device": "",
            "session_name": "",
            "device_model": "",
        },
    )

    with TestClient(app) as client:
        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            instance = create_zapi_instance(
                db,
                name="Limpar Fila",
                phone_label="+55 (55) 91111-1111",
                base_url="https://api.z-api.io",
                instance_id=f"clear-queue-{uuid4().hex}",
                instance_token="clear-queue-token",
                client_token="clear-queue-client",
                created_by_id=admin.id if admin else None,
            )
            db.commit()
            db.refresh(instance)
            _login_admin(client)
            page = client.get("/settings/whatsapp")
            assert page.status_code == 200
            assert "Fila Z-API" in page.text
            assert "Limpar fila Z-API" in page.text
            csrf = _csrf(page.text)

            denied = client.post(
                f"/settings/zapi/instances/{instance.id}/clear-queue",
                data={"csrf_token": csrf, "confirm": ""},
                follow_redirects=False,
            )
            assert denied.status_code == 303

            cleared = client.post(
                f"/settings/zapi/instances/{instance.id}/clear-queue",
                data={"csrf_token": csrf, "confirm": "sim"},
                follow_redirects=False,
            )
            assert cleared.status_code == 303
            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "ZAPI_NATIVE_QUEUE_CLEARED",
                    AuditLog.entity_id == str(instance.id),
                )
                .order_by(AuditLog.id.desc())
            )
            assert audit is not None
            assert '"pending_before": 2' in (audit.payload_json or "")
        finally:
            db.close()
