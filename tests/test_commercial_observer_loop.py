"""Regressao: observador do worker deve consumir SQLAlchemy 2.x Result sem erro."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Contact, Conversation, Message
from app.services.commercial_intelligence import _eligible_recent, analyze_conversation
from app.timeutils import utcnow


def test_observer_selects_only_unanalyzed_conversations_with_sqlalchemy_result():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            contact = Contact(name="Cliente", phone="5555999870011")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, status="EM_FILA",
                team="LOJA", last_message_at=utcnow(),
            )
            db.add(conversation)
            db.flush()
            message = Message(
                contact_id=contact.id,
                conversation_id=conversation.id,
                direction="INBOUND", message_type="TEXT",
                body="Quero contratar", created_at=utcnow(),
            )
            db.add(message)
            db.commit()
            assert _eligible_recent(db, 5) == [conversation.id]
            assert analyze_conversation(db, conversation.id, use_model=False) is True
            db.commit()
            assert _eligible_recent(db, 5) == []
            message2 = Message(
                contact_id=contact.id,
                conversation_id=conversation.id,
                direction="INBOUND", message_type="TEXT",
                body="Podemos fechar", created_at=utcnow(),
            )
            db.add(message2)
            db.commit()
            assert _eligible_recent(db, 5) == [conversation.id]
    finally:
        engine.dispose()
