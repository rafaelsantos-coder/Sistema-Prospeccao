from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import User  # registers the users foreign key
from app.models_instagram import (
    instagram_inbox_members as members,
    instagram_inbox_messages as messages,
    instagram_inbox_threads as threads,
)
from app.services import instagram_inbox as inbox


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    for table in (members, threads, messages):
        table.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def event(mid="m1", sender="201", recipient="101", text="Tenho interesse"):
    return {"sender": {"id": sender}, "recipient": {"id": recipient},
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        "message": {"mid": mid, "text": text}}


def test_disabled_default(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_INBOX_ENABLED", raising=False)
    assert not inbox.enabled()


def test_inbound_encryption_idempotence_and_rejects_other_events(db):
    msg = event()
    assert inbox.record_inbound(db, "101", "messaging", msg)
    assert not inbox.record_inbound(db, "101", "messaging", msg)
    assert not inbox.record_inbound(db, "101", "messaging", event("m2", recipient="102"))
    assert not inbox.record_inbound(db, "101", "messaging", event("m3", sender="101"))
    echo = event("m4")
    echo["message"]["is_echo"] = True
    assert not inbox.record_inbound(db, "101", "messaging", echo)
    assert not inbox.record_inbound(db, "101", "messaging", {"read": {"mid": "m1"}})
    assert inbox.record_inbound(db, "101", "changes", {"field": "messages", "value": event("m5")})
    row = db.execute(select(messages).where(messages.c.body_encrypted.is_not(None))).mappings().all()
    assert len(row) == 2
    assert all("Tenho interesse" not in r["body_encrypted"] for r in row)


def test_queue_privacy_membership_and_claim(db):
    assert inbox.record_inbound(db, "101", "messaging", event())
    key = db.execute(select(threads.c.thread_id)).scalar_one()
    assert inbox.read_thread(db, "101", key, 11) is None
    assert not inbox.claim(db, "101", key, 11)
    db.execute(members.insert(), [{"account_id": "101", "user_id": 11},
                                 {"account_id": "101", "user_id": 12}])
    assert inbox.is_member(db, "101", 11)
    assert not inbox.is_member(db, "102", 11)
    assert inbox.claim(db, "101", key, 11)
    assert not inbox.claim(db, "101", key, 12)
    assert not inbox.claim(db, "102", key, 11)
    assert inbox.read_thread(db, "101", key, 12) is None
    assert inbox.read_thread(db, "102", key, 11, True) is None
    assert inbox.list_threads(db, "101", 12) == []
    assert inbox.read_thread(db, "101", key, 11)[1][0][1] == "Tenho interesse"
    assert inbox.read_thread(db, "101", key, 99, True) is not None
