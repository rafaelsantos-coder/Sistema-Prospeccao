"""Disparo enviado fica guardado, mas só aparece na caixa quando o cliente responde."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, User
from app.security import hash_password
from app.services.conversation_engine import apply_conversation_scope


def _admin(db) -> User:
    user = User(
        name="Oculto Admin",
        email=f"oculto.{uuid4().hex[:8]}@sulnet.local",
        password_hash=hash_password("SenhaOculto@2026"),
        role="ADMIN",
        team="B2B",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def _conversation(db, source: str) -> Conversation:
    contact = Contact(phone=f"5555{uuid4().int % 10**8:08d}", name="Cliente")
    db.add(contact)
    db.flush()
    conversation = Conversation(contact_id=contact.id, team="B2B", source=source, status="AGUARDANDO_CLIENTE")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, contact_id=contact.id, direction="OUTBOUND", message_type="TEXT", body="Oferta", status="SENT"))
    db.flush()
    return conversation


def _in_inbox(db, user, conversation) -> bool:
    stmt = apply_conversation_scope(
        select(Conversation.id).where(Conversation.id == conversation.id), user, inbox=True
    )
    return conversation.id in set(db.scalars(stmt))


def test_disparo_aparece_so_depois_da_resposta_do_cliente():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = _admin(db)
            sources = ["DISPARO:99", "CAMPANHA:99", "TELEGRAM"]
            conversations = [_conversation(db, source) for source in sources]
            for conversation in conversations:
                assert not _in_inbox(db, admin, conversation), conversation.source

            for conversation in conversations:
                db.add(Message(conversation_id=conversation.id, contact_id=conversation.contact_id, direction="INBOUND", message_type="TEXT",
                               body="Tenho interesse", status="RECEIVED"))
            db.flush()
            for conversation in conversations:
                assert _in_inbox(db, admin, conversation), conversation.source
        finally:
            db.rollback()
            db.close()


def test_conversa_iniciada_pelo_vendedor_continua_visivel_sem_resposta():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = _admin(db)
            manual = _conversation(db, "WHATSAPP")
            assert _in_inbox(db, admin, manual)
        finally:
            db.rollback()
            db.close()
