from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.models import AIAgentProfile, AIFollowupJob, AIJob, AIQueueBinding, AIReplyDelivery, Contact, Conversation, Message, ServiceQueue
from app.services.ai_reply_delivery import process_next_ai_reply, queue_ai_reply, reply_statuses
from app.services.ai_sdr import enqueue_ai_job, process_ai_job
from app.services.ai_sdr_policies import _fast_purchase_decision, _fast_purchase_request
from app.services.conversation_engine import register_message, update_message_statuses
from app.services.crm_notes import has_confirmed_activation
from app.services.zapi import ZApiError
from app.timeutils import utcnow


@pytest.fixture
def chat(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.ai_sdr.settings", replace(
            settings, ai_sdr_enabled=True, ai_seller_enabled=False, ai_autonomous_enabled=True,
        ),
    )
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
        contact = Contact(phone="555500000001")
        queue = ServiceQueue(name="Fila IA", code="FILA-IA", team="DIGITAL")
        agent = AIAgentProfile(user_id=1, slug="sulia-teste", execution_mode="AUTO")
        db.add_all([contact, queue, agent])
        db.flush()
        conversation = Conversation(contact_id=contact.id, team="DIGITAL")
        db.add(conversation)
        db.flush()
        db.add(AIQueueBinding(queue_id=queue.id, agent_id=agent.id, handling_mode="AI_FIRST"))
        monkeypatch.setattr("app.services.ai_queue_scope.ai_allowed_for_conversation", lambda *a, **k: True)
        monkeypatch.setattr("app.services.ai_sdr.ai_allowed_for_conversation", lambda *a, **k: True)
        monkeypatch.setattr("app.services.ai_sdr.binding_for_conversation", lambda *_: SimpleNamespace(
            queue_id=queue.id, agent_id=agent.id, agent=agent, handling_mode="AI_FIRST",
        ))
        db.commit()
        yield db, conversation, agent, queue
    engine.dispose()


def _inbound(db, conversation, text):
    message = register_message(
        db, conversation, direction="INBOUND", message_type="TEXT", body=text, status="RECEIVED",
    )
    db.flush()
    return message


def test_menu_after_human_pauses_jobs_and_followups_then_resumes(chat):
    db, conversation, agent, queue = chat
    original = _inbound(db, conversation, "Quero saber os planos")
    job = enqueue_ai_job(db, conversation=conversation, message=original)
    assert job
    followup = AIFollowupJob(
        conversation_id=conversation.id, agent_id=agent.id, queue_id=queue.id, source_message_id=original.id,
        due_at=utcnow(),
    )
    db.add(followup)
    db.flush()

    automatic = _inbound(db, conversation, "Opção de menu inválida.")
    assert enqueue_ai_job(db, conversation=conversation, message=automatic) is None
    assert followup.status == "CANCELADO"
    assert process_ai_job(db, job) == "skipped"
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Aguardando pessoa"

    person = _inbound(db, conversation, "Quero um plano de fibra")
    assert enqueue_ai_job(db, conversation=conversation, message=person)


def test_response_retries_only_confirmed_failures_and_preserves_buttons(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero conhecer os planos")
    buttons = [{"id": "fibra", "label": "Fibra"}, {"id": "movel", "label": "Móvel"}]
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Qual produto?", buttons=buttons,
    )
    assert queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Resposta duplicada",
    ).id == pending.id
    db.commit()
    delivery = db.scalar(select(AIReplyDelivery))
    attempts = []

    def send(**payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise ZApiError("Limite temporário.", status_code=429)
        return {"messageId": "sulia-reply"}

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_button_list=send),
    )
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Aguardando IA"
    assert process_next_ai_reply(db)
    assert pending.status == "FAILED"
    assert reply_statuses(db, [conversation.id])[conversation.id]["attention"]
    assert not process_next_ai_reply(db)
    delivery.available_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert process_next_ai_reply(db)
    assert pending.status == "SENT"
    assert attempts[0] == attempts[1]
    assert attempts[1]["buttons"] == buttons
    assert db.scalar(select(func.count()).select_from(AIReplyDelivery)) == 1

    update_message_statuses(db, {"status": "RECEIVED", "ids": ["sulia-reply"]})
    db.commit()
    update_message_statuses(db, {"status": "SENT", "ids": ["sulia-reply"]})
    db.commit()
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Entregue"
    assert not process_next_ai_reply(db)


def test_missing_http_confirmation_never_retries_automatically(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero saber o preço")
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Qual a sua cidade?",
    )
    db.commit()
    attempts = []

    def send(**payload):
        attempts.append(payload)
        raise ZApiError("Confirmação indisponível.") from httpx.ReadTimeout("timeout")

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_text=send),
    )
    assert process_next_ai_reply(db)
    assert pending.status == "UNKNOWN"
    delivery = db.scalar(select(AIReplyDelivery))
    delivery.available_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert not process_next_ai_reply(db)
    assert len(attempts) == 1


def test_provider_callback_can_precede_send_result(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero ver as opções")
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Você prefere fibra ou móvel?",
    )
    db.commit()

    def send(**_payload):
        update_message_statuses(db, {"status": "READ", "ids": ["early-id"]})
        db.flush()
        return {"messageId": "early-id"}

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_text=send),
    )
    assert process_next_ai_reply(db)
    assert pending.status == "READ"
    assert pending.delivered_at
    assert pending.read_at


@pytest.mark.parametrize(("text", "offer", "expected"), [
    ("Quero contratar", "Em Santa Rosa temos fibra de 800 Mega por R$ 119,90. Quer contratar?", "FTTH_800"),
    ("Sim", "Sulmóvel 5GB por R$ 44,90. Quer contratar agora?", "SULMOVEL_5GB"),
    ("Quero contratar", "650 Mega por R$ 99,90 ou 800 Mega por R$ 119,90.", None),
    ("Quero contratar se for mais barato", "Fibra de 800 Mega por R$ 119,90.", None),
    ("Sim", "Temos fibra de 800 Mega por R$ 119,90. Você já é cliente?", None),
])
def test_purchase_acceptance_uses_the_offer_the_customer_received(text, offer, expected):
    request = _fast_purchase_request({
        "latest_customer_message": text,
        "history": [{"direction": "OUTBOUND", "body": offer}, {"direction": "INBOUND", "body": text}],
        "confirmed_facts": {"city": "Santa Rosa"},
    })
    if expected is None:
        assert request is None
    else:
        assert request["plan"]["code"] == expected
        decision = _fast_purchase_decision(request["plan"], request["city"], handoff=True)
        assert decision.customer_commitment and decision.handoff_target == "CLOSER_AI"


def test_acceptance_without_plan_in_latest_message_bypasses_model(chat, monkeypatch):
    db, conversation, agent, queue = chat
    register_message(
        db, conversation, direction="OUTBOUND", message_type="TEXT",
        body="Em Santa Rosa temos fibra de 800 Mega por R$ 119,90. Quer contratar agora?",
        sender_user_id=agent.user_id, status="SENT",
    )
    original = _inbound(db, conversation, "Quero contratar")
    job = AIJob(conversation_id=conversation.id, message_id=original.id, agent_id=agent.id, queue_id=queue.id)
    db.add(job)
    db.flush()
    observed = []
    monkeypatch.setattr(
        "app.services.ai_sdr.build_ai_context",
        lambda *_a, **_k: {
            "latest_customer_message": original.body,
            "history": [
                {"direction": "OUTBOUND", "body": "Fibra de 800 Mega por R$ 119,90. Quer contratar?"},
                {"direction": "INBOUND", "body": original.body},
            ],
            "confirmed_facts": {"city": "Santa Rosa"},
        },
    )
    monkeypatch.setattr(
        "app.services.ai_seller.start_closing_from_sdr",
        lambda *_a, **kwargs: observed.append(kwargs["decision"]) or True,
    )
    assert process_ai_job(db, job) == "fast_handoff"
    assert observed[0].offer_plan_code == "Fibra 800 Mega"
    assert observed[0].offer_price == 119.90


def test_activation_requires_an_explicit_crm_confirmation():
    assert not has_confirmed_activation({
        "opportunity": {"status": "won"}, "contract": {"status": "assinado"},
    })
    assert not has_confirmed_activation({"activation": {"status": "agendada"}})
    assert has_confirmed_activation({"opportunity": {"activationStatus": "Ativada"}})
