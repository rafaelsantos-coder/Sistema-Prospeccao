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


def test_reopened_conversation_reply_is_attributed_by_phone():
    """Uma nova conversa/instância não pode zerar a resposta à campanha."""
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Atribuição por contato", channel="WHATSAPP",
                status="APROVADA", team="DIGITAL",
            )
            queue = ServiceQueue(
                name="Fila Atribuição", code="DISPATCH-METRICS-ATTR",
                team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id, status="ATIVO")
            db.add(dispatch)
            db.flush()
            contact = Contact(phone="5555999929905", name="Cliente Reaberto")
            db.add(contact)
            db.flush()
            old_conversation = Conversation(
                contact_id=contact.id, source=f"DISPARO:{dispatch.id}", status="ENCERRADO",
            )
            new_conversation = Conversation(
                contact_id=contact.id, source="WHATSAPP", status="EM_FILA",
            )
            db.add_all([old_conversation, new_conversation])
            db.flush()
            sent_at = utcnow()
            db.add(CampaignDispatchRecipient(
                dispatch_id=dispatch.id, name="Cliente Reaberto",
                phone=contact.phone, conversation_id=old_conversation.id,
                status="ENVIADO", sent_at=sent_at, provider_message_id="ATTR-REOPEN-01",
            ))
            db.add(Message(
                conversation_id=new_conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT", body="Tenho interesse",
                created_at=sent_at + timedelta(seconds=30),
            ))
            db.flush()
            counts = dispatch_counts(db, dispatch.id)
            assert counts["replied"] == 1
            assert counts["sent"] == 1
            assert counts["delivered"] == 0
            assert counts["unconfirmed"] == 1
            db.rollback()


def test_successive_dispatches_attribute_each_reply_only_to_latest_send():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(
                name="Atribuição sucessiva", channel="WHATSAPP", status="APROVADA",
            )
            queue = ServiceQueue(
                name="Fila Atribuição Sucessiva", code="DISPATCH-METRICS-SUCCESSIVE",
                team="DIGITAL", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            d1 = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id, status="ATIVO")
            d2 = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id, status="ATIVO")
            db.add_all([d1, d2])
            db.flush()
            contact = Contact(phone="5555999929906", name="Cliente Campanhas Sucessivas")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, source="WHATSAPP")
            db.add(conversation)
            db.flush()
            now = utcnow()
            db.add_all([
                CampaignDispatchRecipient(
                    dispatch_id=d1.id, phone=contact.phone, name="Cliente",
                    conversation_id=None, status="ENVIADO", sent_at=now,
                ),
                CampaignDispatchRecipient(
                    dispatch_id=d2.id, phone=contact.phone, name="Cliente",
                    conversation_id=conversation.id, status="ENVIADO",
                    sent_at=now + timedelta(minutes=5),
                ),
                Message(
                    conversation_id=conversation.id, contact_id=contact.id,
                    direction="INBOUND", message_type="TEXT", body="Quero saber mais",
                    created_at=now + timedelta(minutes=6),
                ),
            ])
            db.flush()
            counts = dispatch_counts_many(db, [d1.id, d2.id])
            assert counts[d1.id]["replied"] == 0
            assert counts[d2.id]["replied"] == 1

            db.add(Message(
                conversation_id=conversation.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT", body="Outra resposta",
                created_at=now + timedelta(minutes=8),
            ))
            db.flush()
            counts = dispatch_counts_many(db, [d1.id, d2.id])
            assert counts[d1.id]["replied"] == 0
            assert counts[d2.id]["replied"] == 1
            db.rollback()


def test_delivery_metrics_distinguish_api_acceptance_from_provider_receipt():
    with TestClient(app):
        with SessionLocal() as db:
            campaign = Campaign(name="Recebimento real", channel="WHATSAPP", status="ATIVA")
            queue = ServiceQueue(name="Fila Entrega", code="DISPATCH-METRICS-DELIVERY", team="DIGITAL", active=True)
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id, status="ATIVO")
            db.add(dispatch)
            db.flush()
            now = utcnow()
            for i, status, delivered, read in [
                (7, "READ", True, True),
                (8, "RECEIVED", True, False),
                (9, "FAILED", False, False),
                (10, "SENT", False, False),
            ]:
                phone = f"55559999299{i:02d}"
                contact = Contact(phone=phone, name=f"Destinatário {i}")
                db.add(contact)
                db.flush()
                conv = Conversation(contact_id=contact.id, source="WHATSAPP")
                db.add(conv)
                db.flush()
                provider_id = f"DISPATCH-DELIVERY-{i}"
                db.add(CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, phone=phone, name=contact.name,
                    status="ENVIADO", sent_at=now,
                    conversation_id=conv.id, provider_message_id=provider_id,
                ))
                db.add(Message(
                    conversation_id=conv.id, contact_id=contact.id,
                    provider_message_id=provider_id, direction="OUTBOUND",
                    message_type="TEXT", body="Oferta", status=status,
                    delivered_at=now if delivered else None,
                    read_at=now if read else None,
                ))
            db.flush()
            counts = dispatch_counts(db, dispatch.id)
            assert counts["sent"] == 4
            assert counts["delivered"] == 2
            assert counts["read"] == 1
            assert counts["provider_failed"] == 1
            assert counts["unconfirmed"] == 1
            assert counts["replied"] == 0
            db.rollback()
