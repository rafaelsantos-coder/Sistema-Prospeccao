"""Quem enxerga cada conversa na caixa de entrada.

Regressão: vendedores (CONSULTOR) viam em "Ativas" todos os disparos e
campanhas da fila, porque a conversa nasce sem dono e vinculada à fila.
Vendedor só deve ver o que é dele (o carrossel entrega); disparos são do admin.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIConversationState,
    Contact,
    Conversation,
    ConversationQueueLink,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.conversation_engine import apply_conversation_scope


def _user(db, email: str, role: str) -> User:
    user = User(
        name=email.split("@")[0],
        email=email,
        password_hash=hash_password("SenhaVisivel@2026"),
        role=role,
        team="VISIB",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def _conversation(db, phone: str, *, queue: ServiceQueue, owner: User | None = None,
                  status: str = "AGUARDANDO_CLIENTE", unread: int = 0) -> Conversation:
    contact = Contact(phone=phone, name=phone)
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id,
        assigned_user_id=owner.id if owner else None,
        team="VISIB",
        source="DISPARO:1",
        status=status,
        unread_count=unread,
    )
    db.add(conversation)
    db.flush()
    db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue.id, origin="DISPATCH"))
    db.flush()
    return conversation


def _visible(db, user: User, ids: list[int], *, inbox: bool = True) -> set[int]:
    stmt = apply_conversation_scope(
        select(Conversation.id).where(Conversation.id.in_(ids)),
        user,
        inbox=inbox,
    )
    return set(db.scalars(stmt))


def test_inbox_visibility_by_role():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = _user(db, "visib.admin@sulnet.local", "ADMIN")
            seller = _user(db, "visib.claudia@sulnet.local", "CONSULTOR")
            other = _user(db, "visib.outro@sulnet.local", "CONSULTOR")
            supervisor = _user(db, "visib.super@sulnet.local", "SUPERVISOR")

            carousel = ServiceQueue(name="Fila carrossel", code="VISIB-RR", team="VISIB",
                                    routing_strategy="ROUND_ROBIN", active=True)
            manual = ServiceQueue(name="Fila manual", code="VISIB-MANUAL", team="VISIB",
                                  routing_strategy="MANUAL", active=True)
            db.add_all([carousel, manual])
            db.flush()
            for queue in (carousel, manual):
                db.add_all([
                    ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True),
                    ServiceQueueMember(queue_id=queue.id, user_id=other.id, active=True),
                ])
            db.flush()

            dispatch = _conversation(db, "5555900000001", queue=carousel)
            dispatch_replied_ai = _conversation(db, "5555900000002", queue=carousel,
                                                status="EM_FILA", unread=1)
            mine = _conversation(db, "5555900000003", queue=carousel, owner=seller,
                                 status="ATRIBUIDO", unread=1)
            others = _conversation(db, "5555900000004", queue=carousel, owner=other,
                                   status="EM_ATENDIMENTO")
            manual_dispatch = _conversation(db, "5555900000005", queue=manual)
            manual_handoff = _conversation(db, "5555900000006", queue=manual, status="EM_FILA")
            manual_replied = _conversation(db, "5555900000007", queue=manual,
                                           status="EM_FILA", unread=2)
            agent = db.scalar(select(AIAgentProfile).limit(1))
            if agent:
                db.add(AIConversationState(conversation_id=manual_handoff.id,
                                           agent_id=agent.id, state="HANDOFF"))
                db.flush()

            ids = [c.id for c in (dispatch, dispatch_replied_ai, mine, others,
                                  manual_dispatch, manual_handoff, manual_replied)]

            assert _visible(db, admin, ids) == set(ids)

            seller_sees = _visible(db, seller, ids)
            assert mine.id in seller_sees
            assert dispatch.id not in seller_sees
            assert dispatch_replied_ai.id not in seller_sees
            assert others.id not in seller_sees
            assert manual_dispatch.id not in seller_sees
            assert manual_replied.id in seller_sees
            if agent:
                assert manual_handoff.id in seller_sees

            assert _visible(db, supervisor, ids) == {mine.id, others.id}
            assert _visible(db, supervisor, ids, inbox=False) == set(ids)
        finally:
            db.rollback()
            db.close()
