"""Métricas de disparo: entrega por zaapId, resposta com 9º dígito e status atrasado."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Contact,
    Conversation,
    Message,
    MessageEvent,
    ServiceQueue,
)
from app.services.dispatch_queue import (
    dispatch_counts,
    process_dispatch_recipient,
)
from app.services.scoring import brazilian_mobile_alternate, phone_match_keys
from app.services.zapi import ZApiService
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance
from app.timeutils import utcnow


def test_phone_match_keys_covers_ninth_digit_variants():
    with_nine = "5555999991234"
    without_nine = "555599991234"
    assert brazilian_mobile_alternate(with_nine) == without_nine
    assert brazilian_mobile_alternate(without_nine) == with_nine
    assert phone_match_keys(with_nine) == {with_nine, without_nine}
    assert phone_match_keys(without_nine) == {with_nine, without_nine}


def test_delivery_counts_when_status_matched_only_via_zaap_id():
    """Webhook atualizou Message pelo zaapId; destinatário só guarda messageId."""
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Zaap", channel="WHATSAPP", status="APROVADA", team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila Zaap", code="DISPATCH-ZAAP-JOIN", team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
            )
            db.add(dispatch)
            db.flush()
            contact = Contact(phone="5555999911111", name="Cliente Zaap")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, source=f"DISPARO:{dispatch.id}")
            db.add(conversation)
            db.flush()
            now = utcnow()
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="OUTBOUND",
                    message_type="TEXT",
                    body="Oferta",
                    provider_message_id="MSG-ZAAP-JOIN-1",
                    provider_zaap_id="ZAAP-ONLY-STATUS-1",
                    status="RECEIVED",
                    delivered_at=now,
                    sent_at=now,
                )
            )
            db.add(
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id,
                    name="Cliente Zaap",
                    phone=contact.phone,
                    conversation_id=conversation.id,
                    status="ENVIADO",
                    sent_at=now,
                    # Destinatário guarda o messageId; status chegou com o zaapId
                    # (já aplicado na Message via provider_zaap_id).
                    provider_message_id="ZAAP-ONLY-STATUS-1",
                )
            )
            db.flush()
            counts = dispatch_counts(db, dispatch.id)
            assert counts["sent"] == 1
            assert counts["delivered"] == 1
            assert counts["unconfirmed"] == 0
            db.rollback()


def test_reply_counts_when_inbound_phone_differs_by_ninth_digit():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha 9 digito", channel="WHATSAPP",
                status="APROVADA", team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila 9 digito", code="DISPATCH-NINTH", team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
            )
            db.add(dispatch)
            db.flush()
            # Contato/webhook sem o 9; CSV/disparo com o 9.
            contact = Contact(phone="555599991234", name="Cliente Sem 9")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, source="WHATSAPP")
            db.add(conversation)
            db.flush()
            sent_at = utcnow()
            db.add(
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id,
                    name="Cliente Com 9",
                    phone="5555999991234",
                    conversation_id=None,
                    status="ENVIADO",
                    sent_at=sent_at,
                    provider_message_id="NINTH-1",
                )
            )
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body="Resposta automática fora do horário",
                    created_at=sent_at + timedelta(minutes=2),
                )
            )
            db.flush()
            counts = dispatch_counts(db, dispatch.id)
            assert counts["replied"] == 1
            db.rollback()


def test_dispatch_reconciles_early_status_webhook(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "EARLY-STATUS-MSG-1",
            "zaapId": "EARLY-STATUS-ZAAP-1",
        },
    )

    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Early Status", channel="WHATSAPP",
                status="APROVADA", team="DIGITAL",
                sequence_json='[{"message":"Olá"}]',
            )
            queue = ServiceQueue(
                name="Fila Early", code="DISPATCH-EARLY", team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            instance = create_zapi_instance(
                db,
                name="WhatsApp Early",
                phone_label="Early",
                base_url="https://api.z-api.io",
                instance_id="instance-early-status",
                instance_token="token-early-status",
                client_token="client-early-status",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=30, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Cliente Early",
                phone="5555999988771",
                message_body="Olá",
                status="ENVIANDO",
            )
            db.add(recipient)
            earlier = utcnow() - timedelta(seconds=3)
            db.add(
                MessageEvent(
                    message_id=None,
                    provider_message_id="EARLY-STATUS-MSG-1",
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
                select(Message).where(Message.provider_message_id == "EARLY-STATUS-MSG-1")
            )
            assert message is not None
            assert message.read_at is not None
            assert message.delivered_at is not None
            event = db.scalar(
                select(MessageEvent).where(
                    MessageEvent.provider_message_id == "EARLY-STATUS-MSG-1"
                )
            )
            assert event.event_type == "STATUS"
            assert event.message_id == message.id
            counts = dispatch_counts(db, dispatch.id)
            assert counts["delivered"] == 1
            assert counts["read"] == 1
