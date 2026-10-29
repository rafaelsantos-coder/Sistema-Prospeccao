from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, ServiceQueue, ServiceQueueMember, User
from app.security import hash_password
from app.services.media_storage import MediaStorage
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


def test_transferred_consultant_sees_full_contact_history(monkeypatch):
    password = "SenhaFila@2026"
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/historico.jpg",
    )

    with TestClient(app) as owner_client, TestClient(app) as target_client:
        db = SessionLocal()
        try:
            owner = _consultant(
                db,
                "history-owner@sulnet.local",
                "Atendente Histórico Origem",
            )
            target = _consultant(
                db,
                "history-target@sulnet.local",
                "Atendente Histórico Destino",
            )
            queue = ServiceQueue(
                name="Fila Histórico Permanente",
                code="QUEUE-HISTORY-TRANSFER",
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
                phone="5555988877991",
                name="Cliente Histórico",
                business_name="Cliente Histórico",
            )
            db.add(contact)
            db.flush()

            old_conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=owner.id,
                team="DIGITAL",
                status="ENCERRADO",
                opened_at=utcnow(),
                closed_at=utcnow(),
            )
            db.add(old_conversation)
            db.flush()
            db.add_all(
                [
                    Message(
                        conversation_id=old_conversation.id,
                        contact_id=contact.id,
                        sender_user_id=owner.id,
                        direction="OUTBOUND",
                        message_type="TEXT",
                        body="Mensagem antiga antes da transferência",
                        status="SENT",
                    ),
                    Message(
                        conversation_id=old_conversation.id,
                        contact_id=contact.id,
                        sender_user_id=owner.id,
                        direction="INTERNAL",
                        message_type="NOTE",
                        body="Nota interna antiga importante",
                        status="INTERNAL",
                    ),
                    Message(
                        conversation_id=old_conversation.id,
                        contact_id=contact.id,
                        sender_user_id=owner.id,
                        direction="OUTBOUND",
                        message_type="IMAGE",
                        body="Foto antiga",
                        media_url="s3://conversationmedia/history/foto.jpg",
                        mime_type="image/jpeg",
                        status="SENT",
                    ),
                ]
            )

            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=owner.id,
                team="DIGITAL",
                status="EM_ATENDIMENTO",
                opened_at=utcnow(),
            )
            db.add(conversation)
            db.flush()
            bind_conversation_queue(db, conversation.id, queue.id, origin="TEST")
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body="Mensagem atual do cliente",
                    status="RECEIVED",
                )
            )
            join_whatsapp(db, target)
            db.commit()
            conversation_id = conversation.id
            target_id = target.id
            old_media_id = db.scalar(
                select(Message.id).where(
                    Message.conversation_id == old_conversation.id,
                    Message.message_type == "IMAGE",
                )
            )
        finally:
            db.close()

        token = _login(owner_client, "history-owner@sulnet.local", password)
        joined = owner_client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.status_code == 200

        page = owner_client.get(f"/conversations/{conversation_id}")
        token = _csrf(page.text)
        transferred = owner_client.post(
            f"/conversations/{conversation_id}/assign",
            data={
                "csrf_token": token,
                "assigned_user_id": str(target_id),
            },
            follow_redirects=False,
        )
        assert transferred.status_code == 303

        target_token = _login(target_client, "history-target@sulnet.local", password)
        target_client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": target_token},
        )
        history_page = target_client.get(f"/conversations/{conversation_id}")
        assert history_page.status_code == 200
        assert "Mensagem antiga antes da transferência" in history_page.text
        assert "Nota interna antiga importante" in history_page.text
        assert "Mensagem atual do cliente" in history_page.text
        assert "Foto antiga" in history_page.text

        api = target_client.get(
            f"/api/conversations/{conversation_id}/messages?after_id=0"
        )
        assert api.status_code == 200
        bodies = [item["body"] for item in api.json()["items"]]
        assert "Mensagem antiga antes da transferência" in bodies
        assert "Nota interna antiga importante" in bodies
        assert "Mensagem atual do cliente" in bodies

        media = target_client.get(
            f"/conversations/messages/{old_media_id}/media",
            follow_redirects=False,
        )
        assert media.status_code == 307
        assert media.headers["location"] == "https://signed.example/historico.jpg"


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
