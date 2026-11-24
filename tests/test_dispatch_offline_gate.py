"""Disparos não devem gastar a lista com WhatsApp offline; status atrasado deve reconciliar."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog,
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Message,
    MessageEvent,
    ServiceQueue,
)
from app.services.dispatch_monitor import dispatch_live_snapshot
from app.services.dispatch_queue import process_dispatch_recipient
from app.services.zapi import ZApiService
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance
from app.timeutils import utcnow


def _setup_active_recipient(db, code: str):
    campaign = Campaign(
        name=f"Campanha Offline {code}",
        channel="WHATSAPP",
        status="APROVADA",
        team="DIGITAL",
        sequence_json='[{"message":"Olá"}]',
    )
    queue = ServiceQueue(
        name=f"Fila Offline {code}",
        code=f"OFFLINE-{code}",
        team="DIGITAL",
        active=True,
    )
    db.add_all([campaign, queue])
    db.flush()
    instance = create_zapi_instance(
        db,
        name=f"WhatsApp Offline {code}",
        phone_label=f"Offline {code}",
        base_url="https://api.z-api.io",
        instance_id=f"instance-offline-{code}",
        instance_token=f"token-offline-{code}",
        client_token=f"client-offline-{code}",
        created_by_id=None,
    )
    bind_queue_instance(db, queue.id, instance.id)
    dispatch = CampaignDispatch(
        campaign_id=campaign.id,
        queue_id=queue.id,
        status="ATIVO",
        interval_seconds=30,
        daily_limit=100,
    )
    db.add(dispatch)
    db.flush()
    recipient = CampaignDispatchRecipient(
        dispatch_id=dispatch.id,
        name="Cliente Offline",
        phone=f"55559999{code[-4:].zfill(4)}",
        message_body="Mensagem de teste",
        status="ENVIANDO",
    )
    db.add(recipient)
    db.commit()
    return dispatch, recipient


def test_dispatch_pauses_when_whatsapp_session_offline(monkeypatch):
    sent: list[str] = []

    def fake_send(self, *, phone, message):
        sent.append(phone)
        return {"messageId": "SHOULD-NOT-SEND", "zaapId": "SHOULD-NOT-SEND"}

    monkeypatch.setattr(ZApiService, "send_text", fake_send)
    monkeypatch.setattr(
        ZApiService,
        "status",
        lambda self: {
            "configured": True,
            "connected": False,
            "smartphone_connected": False,
            "error": "disconnected",
        },
    )

    with TestClient(app):
        with SessionLocal() as db:
            dispatch, recipient = _setup_active_recipient(db, "1001")
            outcome = process_dispatch_recipient(db, recipient)
            db.commit()

            assert outcome == "whatsapp_offline_paused"
            assert sent == []
            assert recipient.status == "PENDENTE"
            assert recipient.attempts == 0
            assert "WhatsApp da fila está offline" in (recipient.error or "")
            assert db.get(CampaignDispatch, dispatch.id).status == "PAUSADO"
            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "DISPATCH_WHATSAPP_OFFLINE_PAUSED",
                    AuditLog.entity_id == dispatch.id,
                )
                .limit(1)
            )
            assert audit is not None
            snap = dispatch_live_snapshot(db, db.get(CampaignDispatch, dispatch.id))
            assert snap["offline_pause"] is True
            assert "offline" in snap["status_label"].lower()


def test_dispatch_reconciles_status_unmatched_after_send(monkeypatch):
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
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "DISPATCH-RECONCILE-1",
            "zaapId": "DISPATCH-RECONCILE-ZAAP-1",
        },
    )

    with TestClient(app):
        with SessionLocal() as db:
            dispatch, recipient = _setup_active_recipient(db, "1002")
            earlier = utcnow() - timedelta(seconds=5)
            db.add(
                MessageEvent(
                    message_id=None,
                    provider_message_id="DISPATCH-RECONCILE-1",
                    event_type="STATUS_UNMATCHED",
                    status="READ",
                    created_at=earlier,
                )
            )
            db.commit()

            outcome = process_dispatch_recipient(db, recipient)
            db.commit()

            assert outcome == "sent"
            message = db.scalar(
                select(Message).where(
                    Message.provider_message_id == "DISPATCH-RECONCILE-1"
                )
            )
            assert message is not None
            assert message.read_at is not None
            assert message.delivered_at is not None
            assert message.status == "READ"
            event = db.scalar(
                select(MessageEvent).where(
                    MessageEvent.provider_message_id == "DISPATCH-RECONCILE-1"
                )
            )
            assert event.event_type == "STATUS"
            assert event.message_id == message.id


def test_dispatch_continues_when_status_check_fails(monkeypatch):
    """Falha transitória no status não deve pausar o disparo."""

    def boom(self):
        raise RuntimeError("status timeout")

    monkeypatch.setattr(ZApiService, "status", boom)
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "DISPATCH-STATUS-FAIL-1",
            "zaapId": "ZAAP-STATUS-FAIL-1",
        },
    )

    with TestClient(app):
        with SessionLocal() as db:
            dispatch, recipient = _setup_active_recipient(db, "1003")
            outcome = process_dispatch_recipient(db, recipient)
            db.commit()
            assert outcome == "sent"
            assert recipient.status == "ENVIADO"
            assert db.get(CampaignDispatch, dispatch.id).status == "ATIVO"


def test_delivery_warning_when_few_confirmations():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Warning",
                channel="WHATSAPP",
                status="APROVADA",
                team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila Warning",
                code="DELIVERY-WARN",
                team="DIGITAL",
                active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
            )
            db.add(dispatch)
            db.flush()
            now = utcnow()
            for index in range(25):
                db.add(
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        name=f"Lead {index}",
                        phone=f"55558888{index:04d}",
                        status="ENVIADO",
                        sent_at=now,
                        provider_message_id=f"WARN-{index}",
                    )
                )
            db.flush()
            snap = dispatch_live_snapshot(db, dispatch)
            assert snap["delivery_warning"] is True
            assert snap["delivery_rate"] == 0.0
            db.rollback()
