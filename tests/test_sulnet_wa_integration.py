"""Teste de integração isolada do motor Sulnet: sem enviar ao WhatsApp."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Conversation, ConversationSulnetWAInstance, Message, ServiceQueue,
    ServiceQueueSulnetWAInstance, SulnetWAInstance, SulnetWAReceipt,
)
from app.services.sulnet_wa import GatewayConfig, SulnetWAError, bind_queue_instance


def _csrf(text):
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert match
    return match.group(1)


def _login(client):
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/settings/sulnet-wa").text)


def _event(session_id, message_id="msg-123", text="Mensagem de laboratório"):
    event = {
        "source": "SULNET_WA", "sessionId": session_id,
        "from": "5511999999999", "messageId": message_id,
        "text": text, "kind": "conversation",
    }
    event["eventId"] = hashlib.sha256((session_id + "\0" + message_id).encode()).hexdigest()
    return json.dumps(event, separators=(",", ":")).encode()


def _post_event(client, body, signature_key="B" * 48):
    timestamp = str(int(time.time()))
    signature = hmac.new(signature_key.encode(),
                         timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/sulnet-wa", content=body, headers={
        "Content-Length": str(len(body)),
        "X-Sulnet-WA-Timestamp": timestamp,
        "X-Sulnet-WA-Signature": signature,
        "Content-Type": "application/json",
    })


def test_admin_gate_and_disabled_menu():
    with TestClient(app) as client:
        anon = client.get("/settings/sulnet-wa", follow_redirects=False)
        assert anon.status_code == 303
        assert anon.headers["location"] == "/login"
        _login(client)
        page = client.get("/settings/sulnet-wa")
        assert page.status_code == 200
        assert "Motor WhatsApp Sulnet" in page.text
        assert "Desativado" in page.text or "configuração pendente" in page.text


def test_event_requires_signature_and_never_falls_back_to_another_provider(monkeypatch):
    monkeypatch.setenv("SULNET_WA_ENABLED", "true")
    monkeypatch.setenv("SULNET_WA_BASE_URL", "http://sulnet-wa.railway.internal:3000")
    monkeypatch.setenv("SULNET_WA_ALLOWED_HOST", "sulnet-wa.railway.internal")
    monkeypatch.setenv("SULNET_WA_API_KEY", "A" * 48)
    monkeypatch.setenv("SULNET_WA_WEBHOOK_KEY", "B" * 48)
    with TestClient(app) as client:
        suffix = uuid4().hex[:8]
        session_id = "test-" + suffix
        with SessionLocal() as db:
            instance = SulnetWAInstance(
                name="Motor de teste " + suffix,
                session_id=session_id, active=True,
            )
            queue = ServiceQueue(
                name="Fila Teste Sulnet " + suffix,
                code="SW-" + suffix.upper(),
                team="DIGITAL", active=True,
            )
            db.add_all([instance, queue])
            db.flush()
            db.add(ServiceQueueSulnetWAInstance(
                queue_id=queue.id, instance_id=instance.id,
            ))
            db.commit()
            instance_id, queue_id = instance.id, queue.id

        body = _event(session_id, "inbound-" + suffix)
        rejected = client.post("/webhooks/sulnet-wa", content=body, headers={
            "Content-Length": str(len(body)), "X-Sulnet-WA-Timestamp": str(int(time.time())),
            "X-Sulnet-WA-Signature": "0" * 64,
        })
        assert rejected.status_code == 403

        unknown = _post_event(client, _event("different-session", "inbound-" + suffix))
        assert unknown.status_code == 409

        accepted = _post_event(client, body)
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True, "duplicate": False}

        duplicate = _post_event(client, body)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"ok": True, "duplicate": True}

        with SessionLocal() as db:
            receipt = db.get(
                SulnetWAReceipt,
                hashlib.sha256((session_id + "\0" + "inbound-" + suffix).encode()).hexdigest(),
            )
            assert receipt is not None and receipt.instance_id == instance_id
            links = list(db.scalars(select(ConversationSulnetWAInstance).where(
                ConversationSulnetWAInstance.instance_id == instance_id
            )))
            assert len(links) == 1
            conv = db.get(Conversation, links[0].conversation_id)
            assert conv.source == "SULNET_WA"
            messages = list(db.scalars(select(Message).where(
                Message.conversation_id == conv.id
            )))
            assert len(messages) == 1
            assert messages[0].body == "Mensagem de laboratório"
            assert db.scalar(select(ServiceQueueSulnetWAInstance.queue_id).where(
                ServiceQueueSulnetWAInstance.instance_id == instance_id
            )) == queue_id


def test_same_instance_cannot_be_linked_to_two_queues():
    with TestClient(app):
        suffix = uuid4().hex[:8]
        with SessionLocal() as db:
            inst = SulnetWAInstance(name="Unit test " + suffix, session_id="u-" + suffix,
                                    active=True)
            first = ServiceQueue(name="Unit one " + suffix, code="ONE-" + suffix.upper(), active=True)
            second = ServiceQueue(name="Unit two " + suffix, code="TWO-" + suffix.upper(), active=True)
            db.add_all([inst, first, second])
            db.flush()
            bind_queue_instance(db, first.id, inst.id)
            try:
                bind_queue_instance(db, second.id, inst.id)
                raise AssertionError("must deny same instance on two queues")
            except SulnetWAError:
                pass
            db.rollback()
