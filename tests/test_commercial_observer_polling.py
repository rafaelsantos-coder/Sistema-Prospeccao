"""Regressão: polling do observador com SQLAlchemy 2.1 deve retornar ids sem exceção."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Contact, Conversation, ConversationCommercialInsight, Message
from app.services.commercial_intelligence import _eligible_recent
from app.timeutils import utcnow


def test_observer_selects_recent_unanalyzed_conversation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            contact = Contact(phone="5555999920251", name="Teste observador")
            db.add(contact)
            db.flush()
            convo = Conversation(
                contact_id=contact.id,
                status="EM_ATENDIMENTO",
                team="LOJA",
                last_message_at=utcnow(),
            )
            db.add(convo)
            db.flush()
            message = Message(
                conversation_id=convo.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="TEXT",
                body="Tenho interesse no plano",
            )
            db.add(message)
            db.flush()
            assert _eligible_recent(db, limit=3) == [convo.id]

            db.add(ConversationCommercialInsight(
                conversation_id=convo.id,
                last_analyzed_message_id=message.id,
                analyzed_at=utcnow(),
            ))
            db.flush()
            assert _eligible_recent(db, limit=3) == []
    finally:
        engine.dispose()
