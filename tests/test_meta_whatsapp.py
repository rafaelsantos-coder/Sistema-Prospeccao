from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import MetaWhatsAppAccount, ServiceQueue, ServiceQueueMetaAccount, ServiceQueueZApiInstance
from app.services.meta_whatsapp import (
    MetaWhatsAppService,
    bind_queue_meta_account,
    create_meta_account,
    meta_inbound_to_engine_payload,
    queue_meta_account,
)
from app.services.secret_store import decrypt_secret
from app.services.zapi_instances import create_zapi_instance, queue_instance, service_for_queue


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
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
    page = client.get("/settings/whatsapp")
    return _csrf(page.text)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _fake_meta(monkeypatch, *, phone: str = "+55 55 99999-0001"):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _FakeResponse(
                200,
                {
                    "id": "1234567890",
                    "display_phone_number": phone,
                    "verified_name": "Sulnet Oficial",
                    "quality_rating": "GREEN",
                },
            )

        def post(self, url, headers=None, json=None):
            return _FakeResponse(
                200,
                {
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": "5555999999999", "wa_id": "5555999999999"}],
                    "messages": [{"id": "wamid.TESTMETA123"}],
                },
            )

    monkeypatch.setattr("app.services.meta_whatsapp.httpx.Client", _FakeClient)


def test_register_meta_account_and_bind_to_queue(monkeypatch):
    _fake_meta(monkeypatch)
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/settings/meta/accounts",
            data={
                "csrf_token": token,
                "name": f"Meta Oficial {suffix}",
                "phone_label": "",
                "phone_number_id": f"pnid-{suffix}",
                "waba_id": f"waba-{suffix}",
                "access_token": f"token-{suffix}",
                "app_secret": f"secret-{suffix}",
                "verify_token": f"verify-{suffix}",
                "graph_api_version": "v21.0",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        with SessionLocal() as db:
            account = db.scalar(
                select(MetaWhatsAppAccount).where(
                    MetaWhatsAppAccount.phone_number_id == f"pnid-{suffix}"
                )
            )
            assert account is not None
            assert account.active is True
            assert decrypt_secret(account.access_token_encrypted) == f"token-{suffix}"
            assert account.phone_label.startswith("+55")
            account_id = account.id
            page_token = token

        page = client.get("/settings/queues")
        page_token = _csrf(page.text)
        assert "API oficial Meta" in page.text

        create = client.post(
            "/settings/queues",
            data={
                "csrf_token": page_token,
                "name": f"Fila Meta {suffix}",
                "code": f"META-{suffix.upper()}",
                "team": "DIGITAL",
                "routing_strategy": "ROUND_ROBIN",
                "whatsapp_provider": "META",
                "meta_account_id": str(account_id),
                "zapi_instance_id": "",
            },
            follow_redirects=False,
        )
        assert create.status_code == 303

        with SessionLocal() as db:
            queue = db.scalar(
                select(ServiceQueue).where(ServiceQueue.code == f"META-{suffix.upper()}")
            )
            assert queue is not None
            meta = queue_meta_account(db, queue.id)
            assert meta is not None and meta.id == account_id
            assert queue_instance(db, queue.id) is None
            link = db.scalar(
                select(ServiceQueueMetaAccount).where(
                    ServiceQueueMetaAccount.queue_id == queue.id
                )
            )
            assert link is not None
            service = service_for_queue(db, queue.id)
            assert isinstance(service, MetaWhatsAppService)


def test_switching_queue_from_zapi_to_meta_clears_zapi(monkeypatch):
    _fake_meta(monkeypatch)
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            from app.models import User

            admin_user = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            zapi = create_zapi_instance(
                db,
                name=f"ZAPI {suffix}",
                phone_label="ZAPI",
                base_url="https://api.z-api.io",
                instance_id=f"zapi-meta-switch-{suffix}",
                instance_token=f"ztoken-{suffix}",
                client_token=f"ctoken-{suffix}",
                created_by_id=admin_user.id if admin_user else None,
            )
            meta = create_meta_account(
                db,
                name=f"Meta Switch {suffix}",
                phone_label="+55 55 90000-0000",
                phone_number_id=f"switch-pnid-{suffix}",
                waba_id="",
                access_token=f"mtoken-{suffix}",
                app_secret="",
                verify_token=f"v-{suffix}",
                graph_api_version="v21.0",
                created_by_id=admin_user.id if admin_user else None,
            )
            queue = ServiceQueue(
                name=f"Switch {suffix}",
                code=f"SWITCH-{suffix.upper()}",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
                created_by_id=admin_user.id if admin_user else None,
            )
            db.add(queue)
            db.flush()
            from app.services.zapi_instances import bind_queue_instance

            bind_queue_instance(db, queue.id, zapi.id)
            db.commit()
            queue_id = queue.id
            meta_id = meta.id

        detail = client.get(f"/settings/queues/{queue_id}")
        token = _csrf(detail.text)
        response = client.post(
            f"/settings/queues/{queue_id}",
            data={
                "csrf_token": token,
                "name": f"Switch {suffix}",
                "team": "DIGITAL",
                "branch_id": "",
                "routing_strategy": "ROUND_ROBIN",
                "active": "true",
                "whatsapp_provider": "META",
                "meta_account_id": str(meta_id),
                "zapi_instance_id": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        with SessionLocal() as db:
            assert queue_meta_account(db, queue_id) is not None
            assert queue_instance(db, queue_id) is None
            assert (
                db.scalar(
                    select(ServiceQueueZApiInstance).where(
                        ServiceQueueZApiInstance.queue_id == queue_id
                    )
                )
                is None
            )


def test_meta_webhook_verify_and_inbound(monkeypatch):
    _fake_meta(monkeypatch)
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        with SessionLocal() as db:
            from app.models import User

            admin_user = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            account = create_meta_account(
                db,
                name=f"Webhook Meta {suffix}",
                phone_label="+55 55 91111-1111",
                phone_number_id=f"webhook-pnid-{suffix}",
                waba_id="",
                access_token=f"wtoken-{suffix}",
                app_secret="",
                verify_token=f"hub-verify-{suffix}",
                graph_api_version="v21.0",
                created_by_id=admin_user.id if admin_user else None,
            )
            queue = ServiceQueue(
                name=f"Meta Inbox {suffix}",
                code=f"MINBOX-{suffix.upper()}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                is_default_inbound=True,
                active=True,
                created_by_id=admin_user.id if admin_user else None,
            )
            db.add(queue)
            db.flush()
            bind_queue_meta_account(db, queue.id, account.id)
            db.commit()
            webhook_key = account.webhook_key
            verify_token = account.verify_token
            phone_number_id = account.phone_number_id

        challenge = client.get(
            f"/webhooks/meta/{webhook_key}",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": verify_token,
                "hub.challenge": "12345challenge",
            },
        )
        assert challenge.status_code == 200
        assert challenge.text == "12345challenge"

        inbound = client.post(
            f"/webhooks/meta/{webhook_key}",
            json={
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "WABA",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "metadata": {
                                        "display_phone_number": "5555911111111",
                                        "phone_number_id": phone_number_id,
                                    },
                                    "contacts": [
                                        {
                                            "profile": {"name": "Cliente Meta"},
                                            "wa_id": "5555988887777",
                                        }
                                    ],
                                    "messages": [
                                        {
                                            "from": "5555988887777",
                                            "id": f"wamid.INBOUND-{suffix}",
                                            "timestamp": "1710000000",
                                            "type": "text",
                                            "text": {"body": "Olá pela API oficial"},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        )
        assert inbound.status_code == 200
        assert inbound.json().get("ok") is True
        assert inbound.json().get("processed", 0) >= 1


def test_meta_inbound_payload_conversion():
    payload = meta_inbound_to_engine_payload(
        {
            "id": "wamid.1",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "opt_1", "title": "Quero fibra"},
            },
        },
        contact_wa_id="5555999999999",
    )
    assert payload["phone"] == "5555999999999"
    assert payload["fromMe"] is False
    assert payload["buttonsResponseMessage"]["message"] == "Quero fibra"


def test_meta_inbound_does_not_reuse_other_provider_or_meta_account(monkeypatch):
    from app.services.conversation_engine import store_received_payload
    from app.services.meta_whatsapp import conversation_meta_account
    from app.services.queue_routing import queue_for_conversation

    suffix = uuid4().hex[:8]
    phone = "55559" + str(int(suffix, 16)).zfill(8)[-8:]
    with SessionLocal() as db:
        accounts = []
        queues = []
        for idx in range(2):
            account = create_meta_account(
                db, name=f"Meta {suffix}-{idx}", phone_label="",
                phone_number_id=f"pn-{suffix}-{idx}", waba_id="",
                access_token="test-token", app_secret="", verify_token="",
                graph_api_version="v27.0", created_by_id=None,
            )
            queue = ServiceQueue(
                name=f"Digital {suffix}-{idx}", code=f"MET-{suffix}-{idx}",
                team="DIGITAL", routing_strategy="MANUAL", active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_meta_account(db, queue.id, account.id)
            accounts.append(account)
            queues.append(queue)
        def receive(message_id, **kwargs):
            conversation, message, _ = store_received_payload(
                db, {"phone": phone, "fromMe": False,
                     "messageId": f"{suffix}-{message_id}",
                     "text": {"message": "teste"}}, **kwargs,
            )
            assert conversation is not None and message is not None
            return conversation
        original = receive("other")
        first = receive("first", queue_id=queues[0].id, meta_account_id=accounts[0].id)
        second = receive("second", queue_id=queues[1].id, meta_account_id=accounts[1].id)
        repeated = receive("repeat", queue_id=queues[0].id, meta_account_id=accounts[0].id)
        assert len({original.id, first.id, second.id}) == 3
        assert repeated.id == first.id
        assert conversation_meta_account(db, original.id) is None
        assert conversation_meta_account(db, first.id).id == accounts[0].id
        assert conversation_meta_account(db, second.id).id == accounts[1].id
        assert queue_for_conversation(db, first.id).id == queues[0].id
        assert queue_for_conversation(db, second.id).id == queues[1].id
        db.rollback()



def test_meta_legacy_mixed_history_starts_clean_conversation():
    from app.models import Message
    from app.services.conversation_engine import store_received_payload
    from app.services.meta_whatsapp import bind_conversation_meta_account

    suffix = uuid4().hex[:8]
    phone = "55559" + str(int(suffix, 16)).zfill(8)[-8:]
    with SessionLocal() as db:
        account = create_meta_account(
            db, name=f"Meta legacy {suffix}", phone_label="",
            phone_number_id=f"legacy-{suffix}", waba_id="", access_token="test-token",
            app_secret="", verify_token="", graph_api_version="v27.0", created_by_id=None,
        )
        queue = ServiceQueue(name=f"Digital legacy {suffix}", code=f"ML-{suffix}",
                             team="DIGITAL", routing_strategy="MANUAL", active=True)
        db.add(queue)
        db.flush()
        bind_queue_meta_account(db, queue.id, account.id)
        def receive(mid, **kwargs):
            conv, msg, _ = store_received_payload(
                db, {"phone": phone, "fromMe": False, "messageId": mid,
                     "text": {"message": mid}}, **kwargs)
            assert conv and msg
            return conv
        old = receive(f"zapi-old-{suffix}")
        bind_conversation_meta_account(db, old.id, account.id)
        clean = receive(f"wamid.clean-{suffix}", queue_id=queue.id, meta_account_id=account.id)
        repeat = receive(f"wamid.repeat-{suffix}", queue_id=queue.id, meta_account_id=account.id)
        other = receive(f"zapi-new-{suffix}")
        assert old.id != clean.id
        assert repeat.id == clean.id
        assert other.id != clean.id
        assert len(list(db.scalars(select(Message).where(Message.conversation_id == clean.id)))) == 2
        assert db.scalar(select(Message).where(Message.conversation_id == old.id,
                                               Message.provider_message_id == f"zapi-old-{suffix}")) is not None
        db.rollback()


def test_zapi_seed_preserves_meta_queue(monkeypatch):
    from types import SimpleNamespace
    from app.services import zapi_instances

    suffix = uuid4().hex[:8]
    monkeypatch.setattr(zapi_instances, "settings", SimpleNamespace(
        integrations={"zapi": True},
        zapi_base_url="https://api.z-api.io",
        zapi_instance_id=f"seed-zapi-{suffix}",
        zapi_instance_token="test-instance-token",
        zapi_client_token="test-client-token",
    ))
    with SessionLocal() as db:
        try:
            account = create_meta_account(
                db, name=f"Meta seed {suffix}", phone_label="",
                phone_number_id=f"seed-meta-{suffix}", waba_id="",
                access_token="test-token", app_secret="", verify_token="",
                graph_api_version="v21.0", created_by_id=None,
            )
            queue = ServiceQueue(
                name=f"Meta seed {suffix}", code=f"MS-{suffix}",
                team="DIGITAL", routing_strategy="MANUAL", active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_meta_account(db, queue.id, account.id)
            zapi_instances.seed_default_zapi_instance(db, created_by_id=None)
            assert queue_meta_account(db, queue.id).id == account.id
            assert queue_instance(db, queue.id) is None
            zapi_instances.seed_default_zapi_instance(db, created_by_id=None)
            assert queue_meta_account(db, queue.id).id == account.id
            assert queue_instance(db, queue.id) is None
        finally:
            db.rollback()
