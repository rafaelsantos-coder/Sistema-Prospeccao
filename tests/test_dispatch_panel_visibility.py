"""Painel de disparo: alerta de entrega baixa e resposta por telefone na tabela."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Contact,
    Conversation,
    Message,
    ServiceQueue,
)
from app.services.dispatch_monitor import dispatch_live_snapshot
from app.services.dispatch_queue import recipient_reply_flags
from app.timeutils import utcnow


def test_delivery_warning_appears_when_few_confirmations():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Painel", channel="WHATSAPP", status="APROVADA", team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila Painel", code="DISPATCH-PANEL", team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
            )
            db.add(dispatch)
            db.flush()
            now = utcnow()
            for index in range(25):
                db.add(
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        name=f"Lead {index}",
                        phone=f"55557777{index:04d}",
                        status="ENVIADO",
                        sent_at=now,
                        provider_message_id=f"PANEL-{index}",
                    )
                )
            db.flush()
            snap = dispatch_live_snapshot(db, dispatch)
            assert snap["delivery_warning"] is True
            assert snap["delivery_rate"] == 0.0
            db.rollback()


def test_recipient_reply_flags_match_ninth_digit_variant():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Flag", channel="WHATSAPP", status="APROVADA", team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila Flag", code="DISPATCH-FLAG", team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
            )
            db.add(dispatch)
            db.flush()
            # 55 + DDD 55 + número: com 9º dígito vs sem (12↔13).
            contact = Contact(phone="555588881234", name="Sem nove")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, source="WHATSAPP")
            db.add(conversation)
            db.flush()
            sent_at = utcnow()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Com nove",
                phone="5555988881234",
                status="ENVIADO",
                sent_at=sent_at,
                provider_message_id="FLAG-1",
            )
            other = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Sem resposta",
                phone="5555988899999",
                status="ENVIADO",
                sent_at=sent_at,
                provider_message_id="FLAG-2",
            )
            db.add_all([recipient, other])
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body="Oi",
                    created_at=sent_at + timedelta(minutes=1),
                )
            )
            db.flush()
            flags = recipient_reply_flags(db, [recipient, other])
            assert flags[recipient.id] is True
            assert flags[other.id] is False
            db.rollback()
