from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, ServiceQueue, ServiceQueueMember, User
from app.security import hash_password
from app.services.queue_routing import bind_conversation_queue
from app.services.whatsapp_presence import join_whatsapp
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/dashboard").text)


def _consultant(db, email: str, name: str, team: str = "DIGITAL") -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    user = User(
        name=name,
        email=email,
        password_hash=hash_password("SenhaFila@2026"),
        role="CONSULTOR",
        team=team,
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def test_queue_member_can_be_added_and_removed_individually():
    with TestClient(app) as client:
        _login(client, "admin@sulnet.local", "Sulnet@2026!")
        db = SessionLocal()
        try:
            user = _consultant(
                db,
                "membro-individual@sulnet.local",
                "Membro Individual",
            )
            queue = ServiceQueue(
                name="Fila Gestão Individual",
                code="QUEUE-MEMBER-INDIVIDUAL",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add(queue)
            db.commit()
            queue_id = queue.id
            user_id = user.id
        finally:
            db.close()

        page = client.get(f"/settings/queues/{queue_id}")
        assert page.status_code == 200
        assert "Adicionar e remover usuários" in page.text
        assert "Membro Individual" in page.text
        token = _csrf(page.text)

        added = client.post(
            f"/settings/queues/{queue_id}/members/{user_id}/add",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert added.status_code == 303

        db = SessionLocal()
        try:
            member = db.scalar(
                select(ServiceQueueMember).where(
                    ServiceQueueMember.queue_id == queue_id,
                    ServiceQueueMember.user_id == user_id,
                )
            )
            assert member is not None
            assert member.active is True
        finally:
            db.close()

        page = client.get(f"/settings/queues/{queue_id}")
        token = _csrf(page.text)
        assert "Remover" in page.text

        removed = client.post(
            f"/settings/queues/{queue_id}/members/{user_id}/remove",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert removed.status_code == 303

        db = SessionLocal()
        try:
            member = db.scalar(
                select(ServiceQueueMember).where(
                    ServiceQueueMember.queue_id == queue_id,
                    ServiceQueueMember.user_id == user_id,
                )
            )
            assert member is not None
            assert member.active is False
        finally:
            db.close()


def test_consultant_can_transfer_conversation_to_online_member_of_same_queue():
    password = "SenhaFila@2026"
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            owner = _consultant(
                db,
                "transfer-owner@sulnet.local",
                "Atendente Origem",
            )
            target = _consultant(
                db,
                "transfer-target@sulnet.local",
                "Atendente Destino",
            )
            queue = ServiceQueue(
                name="Fila Transferência",
                code="QUEUE-TRANSFER-CONVERSATION",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add_all(
                [
                    ServiceQueueMember(queue_id=queue.id, user_id=owner.id, active=True),
                    ServiceQueueMember(queue_id=queue.id, user_id=target.id, active=True),
                ]
            )
            contact = Contact(
                phone="5555988877001",
                name="Cliente Transferência",
                business_name="Cliente Transferência",
            )
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=owner.id,
                team="DIGITAL",
                status="EM_ATENDIMENTO",
                opened_at=utcnow(),
            )
            db.add(conversation)
            db.flush()
            bind_conversation_queue(
                db,
                conversation.id,
                queue.id,
                origin="TEST",
            )
            join_whatsapp(db, target)
            db.commit()
            conversation_id = conversation.id
            owner_id = owner.id
            target_id = target.id
        finally:
            db.close()

        token = _login(client, "transfer-owner@sulnet.local", password)
        joined = client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.status_code == 200
        assert joined.json()["online"] is True

        page = client.get(f"/conversations/{conversation_id}")
        assert page.status_code == 200
        assert "Transferir atendimento" in page.text
        assert "Atendente Destino" in page.text
        token = _csrf(page.text)

        response = client.post(
            f"/conversations/{conversation_id}/assign",
            data={
                "csrf_token": token,
                "assigned_user_id": str(target_id),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/conversations?view=active"

        db = SessionLocal()
        try:
            transferred = db.get(Conversation, conversation_id)
            assert transferred is not None
            assert transferred.assigned_user_id == target_id
            assert transferred.assigned_user_id != owner_id
        finally:
            db.close()
