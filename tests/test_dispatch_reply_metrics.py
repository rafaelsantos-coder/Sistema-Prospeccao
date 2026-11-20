"""Indicador de respostas recebidas após disparos WhatsApp em todas as filas."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign, CampaignDispatch, CampaignDispatchRecipient,
    Contact, Conversation, Message, ServiceQueue,
)
from app.services.dispatch_queue import dispatch_counts, dispatch_counts_many
from app.timeutils import utcnow


def test_reply_counter_is_per_dispatch_and_counts_unique_clients_only():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Campanha Indicador Respostas", channel="WHATSAPP",
                status="APROVADA", team="DIGITAL",
            )
            q1 = ServiceQueue(name="Fila Respostas A", code="REPLY-A", team="DIGITAL", active=True)
            q2 = ServiceQueue(name="Fila Respostas B", code="REPLY-B", team="DIGITAL", active=True)
            db.add_all([campaign, q1, q2])
            db.flush()
            d1 = CampaignDispatch(campaign_id=campaign.id, queue_id=q1.id, status="ATIVO")
            d2 = CampaignDispatch(campaign_id=campaign.id, queue_id=q2.id, status="ATIVO")
            db.add_all([d1, d2])
            db.flush()
            now = utcnow()
            for dispatch, suffix, reply_times in [
                (d1, "1", [-10, 2, 3]),  # antigo + duas respostas posteriores => 1 cliente
                (d1, "2", [-3]),         # mensagem anterior ao disparo => 0
                (d1, "3", []),           # sem resposta
                (d2, "4", [1]),          # outra fila também conta
            ]:
                contact = Contact(phone=f"555599992990{suffix}", name=f"Cliente {suffix}")
                db.add(contact)
                db.flush()
                conversation = Conversation(contact_id=contact.id, source=f"DISPARO:{dispatch.id}")
                db.add(conversation)
                db.flush()
                db.add(CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name=f"Cliente {suffix}",
                    phone=contact.phone, conversation_id=conversation.id,
                    status="ENVIADO", sent_at=now,
                ))
                for minutes in reply_times:
                    db.add(Message(
                        conversation_id=conversation.id, contact_id=contact.id,
                        direction="INBOUND", message_type="AUDIO" if minutes == 3 else "TEXT",
                        body="Resposta", created_at=now + timedelta(minutes=minutes),
                    ))
            db.flush()
            assert dispatch_counts(db, d1.id)["replied"] == 1
            assert dispatch_counts(db, d2.id)["replied"] == 1
            both = dispatch_counts_many(db, [d1.id, d2.id])
            assert both[d1.id]["sent"] == 3
            assert both[d1.id]["replied"] == 1
            assert both[d2.id]["sent"] == 1
            assert both[d2.id]["replied"] == 1
            assert dispatch_counts_many(db, []) == {}
            db.rollback()
