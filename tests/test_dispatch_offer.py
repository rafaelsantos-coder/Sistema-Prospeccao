from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Campaign, CampaignDispatch, CampaignDispatchRecipient, Contact, Conversation, Message, MessageEvent, ServiceQueue
from app.services import dispatch_offer as offer
from app.services.ai_seller import _discount_tag_for
from app.timeutils import utcnow


def test_dispatch_interest_to_crm_contract():
    context = {"latest_customer_message": "Tenho interesse", "lead": {"city": "Não informado"}}
    first = offer.offer_decision(context, 31)
    assert first.reply_text.endswith(offer.ASK_CITY) and "99,90" in first.reply_text and not first.handoff_recommended
    context.update(history=[{"direction": "OUTBOUND", "body": first.reply_text}], latest_customer_message="Santa Rosa")
    quote = offer.offer_decision(context, 31)
    assert quote.reply_text.endswith(offer.CONFIRM) and "99,90" in quote.reply_text
    context.update(history=[{"direction": "OUTBOUND", "body": quote.reply_text}], confirmed_facts={"city": "Santa Rosa"})
    context["latest_customer_message"] = "Não tenho interesse"
    assert offer.offer_decision(context, 31) is None
    context["latest_customer_message"] = "Sim"
    accepted = offer.offer_decision(context, 31)
    assert accepted.handoff_recommended and accepted.free_months == 3
    assert accepted.offer_plan_code == "Fibra 650 Mega"
    state = SimpleNamespace(plan_label=accepted.offer_plan_code, handoff_summary=accepted.summary)
    assert _discount_tag_for(state, {"freeMonths": accepted.free_months}) == "Desconto 3 mensalidades"


def test_reply_requires_actual_dispatch_in_this_conversation(monkeypatch):
    with TestClient(app), SessionLocal() as db:
        q = ServiceQueue(name="Oferta teste", code="OFFER-TEST", team="DIGITAL")
        c = Campaign(name="Oferta teste", channel="WHATSAPP", team="DIGITAL")
        contact = Contact(phone="5555999998844")
        db.add_all([q, c, contact])
        db.flush()
        conversation = Conversation(contact_id=contact.id)
        dispatch = CampaignDispatch(campaign_id=c.id, queue_id=q.id)
        db.add_all([conversation, dispatch])
        db.flush()
        monkeypatch.setattr(offer, "DISPATCH_IDS", {dispatch.id})
        now = utcnow()
        recipient = CampaignDispatchRecipient(dispatch_id=dispatch.id, conversation_id=conversation.id,
            phone=contact.phone, status="ENVIADO", sent_at=now, provider_message_id="offer-original")
        incoming = Message(conversation_id=conversation.id, contact_id=contact.id, direction="INBOUND",
            message_type="TEXT", body="Tenho interesse", created_at=now + timedelta(minutes=3))
        db.add_all([recipient, incoming])
        db.flush()
        assert offer.source_dispatch(db, incoming) == dispatch.id
        recipient.status = "FALHOU"
        db.flush()
        assert offer.source_dispatch(db, incoming) is None
        recipient.status = "ENVIADO"
        event = MessageEvent(message_id=incoming.id, event_type="RECEIVED_CALLBACK",
                             payload_json='{"referenceMessageId":"outra-mensagem"}')
        db.add(event)
        db.flush()
        assert offer.source_dispatch(db, incoming) is None
        event.payload_json = '{"referenceMessageId":"offer-original"}'
        db.flush()
        assert offer.source_dispatch(db, incoming) == dispatch.id

def test_contextual_reminder_after_interest_and_proposal():
    initial = offer.offer_decision({"latest_customer_message": "Como funcionam esses três meses?"}, 31)
    assert offer.pilot_followup_theme(initial.reply_text) == offer.PILOT_CITY
    assert offer.PILOT_DELAYS[offer.PILOT_CITY] == timedelta(minutes=20)
    context = {"latest_customer_message": "Santa Rosa", "history": [
        {"direction": "OUTBOUND", "body": offer.pilot_followup_text(offer.PILOT_CITY)}]}
    quote = offer.offer_decision(context, 31)
    assert offer.pilot_followup_theme(quote.reply_text) == offer.PILOT_QUOTE
    assert offer.PILOT_DELAYS[offer.PILOT_QUOTE] == timedelta(hours=3)
    context["latest_customer_message"] = "Qual o valor? Me chama amanhã"
    assert offer.offer_decision(context, 31) is None
