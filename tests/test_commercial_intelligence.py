"""Regressões da inteligência comercial: segurança, etiquetas e classificações."""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Contact,
    Conversation,
    ConversationCommercialInsight,
    ConversationCommercialTag,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    User,
)
from app.routers.conversations import _inbox_counts, _inbox_query
from app.services.commercial_intelligence import (
    TAG_LABELS,
    analyze_conversation,
    insight_map,
    tag_map,
    update_manual_tag,
)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _message(db, conversation, direction, content):
    item = Message(
        contact_id=conversation.contact_id, conversation_id=conversation.id,
        direction=direction, message_type="TEXT", body=content,
    )
    db.add(item)
    db.flush()
    return item


def test_manual_override_survives_ai_reanalysis():
    engine = _db()
    try:
        with Session(engine) as db:
            contact = Contact(name="Teste", phone="5555999910111")
            user = User(
                name="Vendedora", email="vendedora@example.com",
                password_hash="hash", team="LOJA", role="CONSULTOR",
            )
            db.add_all([contact, user])
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, status="EM_ATENDIMENTO",
                assigned_user_id=user.id, team="LOJA",
            )
            db.add(conversation)
            db.flush()
            _message(db, conversation, "OUTBOUND", "Gostaria de contratar hoje?")
            _message(db, conversation, "INBOUND", "Quero fechar. Como envio os documentos?")
            assert analyze_conversation(db, conversation.id, use_model=False)
            tags = {t["code"] for t in tag_map(db, [conversation.id])[conversation.id]}
            assert {"EM_FECHAMENTO", "ALTO_POTENCIAL", "DOCUMENTOS_PENDENTES"}.issubset(tags)

            update_manual_tag(
                db, conversation_id=conversation.id, code="DOCUMENTOS_PENDENTES",
                enabled=False, changed_by_id=user.id,
            )
            update_manual_tag(
                db, conversation_id=conversation.id, code="RETORNO_SOLICITADO",
                enabled=True, changed_by_id=user.id,
            )
            _message(db, conversation, "INBOUND", "Posso mandar os documentos?")
            assert analyze_conversation(db, conversation.id, use_model=False)
            tags = {t["code"] for t in tag_map(db, [conversation.id])[conversation.id]}
            assert "DOCUMENTOS_PENDENTES" not in tags
            assert "RETORNO_SOLICITADO" in tags
            hidden = db.scalar(select(ConversationCommercialTag).where(
                ConversationCommercialTag.conversation_id == conversation.id,
                ConversationCommercialTag.code == "DOCUMENTOS_PENDENTES",
            ))
            assert hidden.suppressed is True
            assert hidden.source == "MANUAL"
            assert db.get(Conversation, conversation.id).assigned_user_id == user.id
            assert len(TAG_LABELS) >= 12
    finally:
        engine.dispose()


def test_outbound_offer_alone_does_not_create_hot_lead():
    engine = _db()
    try:
        with Session(engine) as db:
            contact = Contact(name="Teste", phone="5555999910222")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, status="EM_FILA")
            db.add(conversation)
            db.flush()
            _message(db, conversation, "OUTBOUND", "Quer fechar a compra agora?")
            analyze_conversation(db, conversation.id, use_model=False)
            insight = db.scalar(select(ConversationCommercialInsight).where(
                ConversationCommercialInsight.conversation_id == conversation.id,
            ))
            assert insight.temperature == "FRIO"
            assert insight.score < 45
            assert insight.stage == "PRIMEIRO_CONTATO"
            tags = {t["code"] for t in tag_map(db, [conversation.id]).get(conversation.id, [])}
            assert "ALTO_POTENCIAL" not in tags
    finally:
        engine.dispose()


def test_filters_by_temperature_and_tag_obey_queue_and_owner():
    engine = _db()
    try:
        with Session(engine) as db:
            user = User(
                name="Gestor", email="gestor@example.com", password_hash="x",
                role="GESTOR", team="LOJA",
            )
            queue1 = ServiceQueue(name="A", code="TEST_A", team="LOJA")
            queue2 = ServiceQueue(name="B", code="TEST_B", team="LOJA")
            db.add_all([user, queue1, queue2])
            db.flush()
            for number, queue, speech in [
                ("5555999910331", queue1, "Quero contratar e fechar agora"),
                ("5555999910332", queue2, "Quero contratar e fechar agora"),
                ("5555999910333", queue1, "Só quero conhecer o preço"),
            ]:
                contact = Contact(name=number, phone=number)
                db.add(contact)
                db.flush()
                convo = Conversation(contact_id=contact.id, status="EM_FILA", team="LOJA")
                db.add(convo)
                db.flush()
                db.add(ConversationQueueLink(conversation_id=convo.id, queue_id=queue.id))
                _message(db, convo, "INBOUND", speech)
                assert analyze_conversation(db, convo.id, use_model=False)
            db.commit()
            hot = _inbox_query(
                db, user, q="", view="active",
                queue_id=queue1.id, temperature="QUENTE",
            )
            assert len(hot) == 1
            tagged = _inbox_query(
                db, user, q="", view="active",
                queue_id=queue1.id, tag_code="EM_FECHAMENTO",
            )
            assert [x.id for x in hot] == [x.id for x in tagged]
            counts = _inbox_counts(db, user, queue_id=queue1.id, temperature="QUENTE")
            assert counts["active"] == 1
            assert len(insight_map(db, [hot[0].id])) == 1
    finally:
        engine.dispose()
