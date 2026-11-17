"""Filtros gerenciais por fila não podem misturar pendências entre filas."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Contact,
    Conversation,
    ConversationQueueLink,
    ServiceQueue,
    User,
)
from app.routers.conversations import (
    _inbox_counts,
    _inbox_query,
    _needs_attention,
    _validate_queue_filter,
)
from fastapi import HTTPException


def test_pending_rule_excludes_closed_and_waiting_client():
    assert _needs_attention("EM_FILA", 0, None)
    assert _needs_attention("EM_ATENDIMENTO", 1, 2)
    assert _needs_attention("AGUARDANDO_INTERNO", 0, 2)
    assert _needs_attention("FOLLOW_UP", 0, 2)
    assert not _needs_attention("AGUARDANDO_CLIENTE", 0, 2)
    assert not _needs_attention("ENCERRADO", 8, None)


def test_manager_queue_filter_counts_and_pending_are_isolated():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            manager = User(
                name="Gestor", email="gestor@example.local", password_hash="x",
                role="GESTOR", team="LOJA", active=True,
            )
            seller = User(
                name="Vendedor", email="vendedor@example.local", password_hash="x",
                role="CONSULTOR", team="LOJA", active=True,
            )
            first = ServiceQueue(name="Fila A", code="A", team="LOJA", active=True)
            second = ServiceQueue(name="Fila B", code="B", team="LOJA", active=True)
            db.add_all([manager, seller, first, second])
            db.flush()
            for phone, queue, state, owner, unread in [
                ("5555999000001", first, "EM_FILA", None, 0),
                ("5555999000002", first, "EM_ATENDIMENTO", seller.id, 2),
                ("5555999000003", first, "AGUARDANDO_CLIENTE", seller.id, 0),
                ("5555999000004", second, "EM_FILA", None, 3),
                ("5555999000005", first, "ENCERRADO", seller.id, 0),
            ]:
                contact = Contact(name=phone, phone=phone)
                db.add(contact)
                db.flush()
                conv = Conversation(
                    contact_id=contact.id, team="LOJA", status=state,
                    assigned_user_id=owner, unread_count=unread,
                )
                db.add(conv)
                db.flush()
                db.add(ConversationQueueLink(conversation_id=conv.id, queue_id=queue.id))
            db.commit()
            counts = _inbox_counts(db, manager, queue_id=first.id)
            assert counts["active"] == 3
            assert counts["pending"] == 2
            assert counts["unread"] == 1
            assert counts["queue"] == 1
            assert counts["closed"] == 1
            assert counts["waiting_client"] == 1
            selected = _inbox_query(
                db, manager, q="", view="pending", queue_id=first.id,
            )
            assert len(selected) == 2
            assert all(c.status != "ENCERRADO" for c in selected)
            other = _inbox_counts(db, manager, queue_id=second.id)
            assert other["active"] == 1
            assert other["pending"] == 1

            _validate_queue_filter(manager, first.id, [{"id": first.id}])
            try:
                _validate_queue_filter(manager, second.id, [{"id": first.id}])
                assert False, "Acesso a fila fora do escopo deve falhar"
            except HTTPException as exc:
                assert exc.status_code == 403
    finally:
        engine.dispose()
