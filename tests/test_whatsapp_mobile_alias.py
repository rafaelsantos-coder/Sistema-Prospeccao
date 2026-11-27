"""Regression: a Brazilian mobile reply must stay in its original WhatsApp chat."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models import Conversation, Message, ServiceQueue, User
from app.services.conversation_engine import (
    ensure_contact, ensure_open_conversation, find_scoped_whatsapp_alias,
    register_message, store_received_payload, whatsapp_mobile_alias,
)
from app.services.meta_whatsapp import create_meta_account, bind_queue_meta_account
from app.services.zapi_instances import create_zapi_instance, bind_queue_instance


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr("app.services.ai_sdr.enqueue_ai_job", lambda *a, **kw: None)
    with TestClient(app), SessionLocal() as db:
        yield db
        db.rollback()


def setup_provider(db, provider):
    suffix = uuid4().hex[:12]
    queue = ServiceQueue(name=f"Fila {suffix}", code=f"ALIAS-{suffix}",
                         team="DIGITAL", routing_strategy="MANUAL", active=True)
    db.add(queue)
    db.flush()
    if provider == "ZAPI":
        inst = create_zapi_instance(
            db, name=f"Zapi {suffix}", phone_label="", base_url="https://api.z-api.io",
            instance_id=f"alias-{suffix}", instance_token="test-token",
            client_token="test-client", created_by_id=None,
        )
        bind_queue_instance(db, queue.id, inst.id)
        return dict(queue_id=queue.id, zapi_instance_id=inst.id)
    account = create_meta_account(
        db, name=f"Meta {suffix}", phone_label="", phone_number_id=f"alias-{suffix}",
        waba_id="", access_token="test-token", app_secret="", verify_token="",
        graph_api_version="v27.0", created_by_id=None,
    )
    bind_queue_meta_account(db, queue.id, account.id)
    return dict(queue_id=queue.id, meta_account_id=account.id)


def receive(db, phone, scope, provider):
    prefix = "wamid." if provider == "META" else ""
    conversation, message, created = store_received_payload(
        db, {"phone": phone, "fromMe": False, "messageId": prefix + uuid4().hex,
             "text": {"message": "Resposta de teste"}},
        **scope,
    )
    assert created and conversation and message
    return conversation, message


def start(db, phone, scope, provider):
    contact = ensure_contact(db, phone)
    chat = ensure_open_conversation(db, contact, source="MANUAL", assign_if_unowned=False,
                                    allow_unbound_reuse=False, **scope)
    owner = db.scalar(select(User).where(User.active.is_(True)).limit(1))
    assert owner
    chat.assigned_user_id = owner.id
    # Meta checks active queue membership on webhook. Create the membership explicitly.
    if provider == "META":
        from app.models import ServiceQueueMember
        db.add(ServiceQueueMember(queue_id=scope["queue_id"], user_id=owner.id, active=True))
    register_message(db, chat, direction="OUTBOUND", message_type="TEXT", body="Mensagem inicial",
                     sender_user_id=owner.id, status="SENT",
                     provider_message_id=("wamid." if provider == "META" else "") + uuid4().hex)
    db.flush()
    return chat


@pytest.mark.parametrize("provider", ["ZAPI", "META"])
@pytest.mark.parametrize("reverse", [False, True])
def test_reply_mobile_alias_stays_in_original_chat(session, provider, reverse):
    scope = setup_provider(session, provider)
    long = "5555981244083"
    short = "555581244083"
    original_phone, callback_phone = (short, long) if reverse else (long, short)
    original = start(session, original_phone, scope, provider)
    owner = original.assigned_user_id
    reply, stored = receive(session, callback_phone, scope, provider)
    assert reply.id == original.id
    assert reply.assigned_user_id == owner
    assert stored.contact_id == original.contact_id
    assert len(list(session.scalars(select(Message).where(
        Message.conversation_id == original.id)))) == 2
    assert find_scoped_whatsapp_alias(session, callback_phone, **scope).id == original.id


def test_alias_not_reused_across_instances_or_providers(session):
    zapi = setup_provider(session, "ZAPI")
    other_zapi = setup_provider(session, "ZAPI")
    meta = setup_provider(session, "META")
    original = start(session, "5555981244083", zapi, "ZAPI")
    other, _ = receive(session, "555581244083", other_zapi, "ZAPI")
    official, _ = receive(session, "555581244083", meta, "META")
    assert len({original.id, other.id, official.id}) == 3
    assert find_scoped_whatsapp_alias(session, "555581244083", **zapi).id == original.id


def test_inbound_only_duplicate_prefers_original_without_moving_old_history(session):
    scope = setup_provider(session, "ZAPI")
    original = start(session, "5555981244083", scope, "ZAPI")
    contact = ensure_contact(session, "555581244083")
    orphan = ensure_open_conversation(session, contact, assign_if_unowned=False, **scope)
    prior = register_message(session, orphan, direction="INBOUND", message_type="TEXT",
                             body="Resposta anterior", status="RECEIVED")
    session.flush()
    assert orphan.id != original.id
    reply, message = receive(session, contact.phone, scope, "ZAPI")
    assert reply.id == original.id and message.conversation_id == original.id
    assert prior.conversation_id == orphan.id
    assert session.get(Conversation, orphan.id) is not None


def test_fixed_number_never_treated_as_mobile_alias():
    assert whatsapp_mobile_alias("5533331234") is None
    assert whatsapp_mobile_alias("555533331234") is None
    assert whatsapp_mobile_alias("5555981244083") == "555581244083"
    assert whatsapp_mobile_alias("555581244083") == "5555981244083"
