from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, User, WhatsAppPresence
from app.security import hash_password
from app.services.conversation_engine import choose_assignee
from app.services.whatsapp_presence import (
    is_user_whatsapp_online,
    join_whatsapp,
)
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str = "admin@sulnet.local", password: str = "Sulnet@2026!") -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    dashboard = client.get("/dashboard")
    return _csrf(dashboard.text)


def test_platform_login_starts_whatsapp_offline():
    with TestClient(app) as client:
        _login(client)
        state = client.get("/api/whatsapp-presence")
        assert state.status_code == 200
        assert state.json()["online"] is False


def test_join_heartbeat_and_leave_whatsapp():
    with TestClient(app) as client:
        token = _login(client)
        joined = client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.status_code == 200
        assert joined.json()["online"] is True

        heartbeat = client.post(
            "/api/whatsapp-presence/heartbeat",
            data={"csrf_token": token},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["online"] is True

        left = client.post(
            "/api/whatsapp-presence/leave",
            data={"csrf_token": token},
        )
        assert left.status_code == 200
        assert left.json()["online"] is False


def test_stale_heartbeat_is_automatically_offline():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            assert admin
            row = join_whatsapp(db, admin)
            row.last_heartbeat_at = utcnow() - timedelta(minutes=5)
            db.commit()
            assert is_user_whatsapp_online(db, admin.id) is False
        finally:
            db.close()


def test_distribution_ignores_offline_user_and_uses_online_group_member():
    with TestClient(app):
        db = SessionLocal()
        try:
            for email in ("offline-b2b@sulnet.local", "online-b2b@sulnet.local"):
                existing = db.scalar(select(User).where(User.email == email))
                if existing:
                    db.delete(existing)
                    db.flush()

            offline = User(
                name="Operador Offline",
                email="offline-b2b@sulnet.local",
                password_hash=hash_password("SenhaTeste@2026"),
                role="CONSULTOR",
                team="B2B",
                active=True,
                must_change_password=False,
            )
            online = User(
                name="Operador Online",
                email="online-b2b@sulnet.local",
                password_hash=hash_password("SenhaTeste@2026"),
                role="CONSULTOR",
                team="B2B",
                active=True,
                must_change_password=False,
            )
            db.add_all([offline, online])
            db.flush()
            join_whatsapp(db, online)

            contact = Contact(
                phone="5555990000001",
                name="Cliente Presença",
                business_name="Empresa Presença",
            )
            db.add(contact)
            db.flush()
            db.commit()

            selected = choose_assignee(db, contact, None)
            assert selected is not None
            assert selected.id == online.id
            assert selected.id != offline.id
        finally:
            db.close()


def test_consultant_chat_is_locked_until_whatsapp_join():
    password = "SenhaTeste@2026"
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == "consultor-presenca@sulnet.local"))
            if not user:
                user = User(
                    name="Consultor Presença",
                    email="consultor-presenca@sulnet.local",
                    password_hash=hash_password(password),
                    role="CONSULTOR",
                    team="B2B",
                    active=True,
                    must_change_password=False,
                )
                db.add(user)
                db.commit()
        finally:
            db.close()

        token = _login(client, "consultor-presenca@sulnet.local", password)
        locked = client.get("/conversations", follow_redirects=False)
        assert locked.status_code == 303
        assert locked.headers["location"] == "/dashboard"

        joined = client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.status_code == 200
        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert "Caixa de entrada" in inbox.text


def test_logout_turns_presence_offline():
    with TestClient(app) as client:
        token = _login(client)
        joined = client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.json()["online"] is True

        dashboard = client.get("/dashboard")
        logout_token = _csrf(dashboard.text)
        response = client.post(
            "/logout",
            data={"csrf_token": logout_token},
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            row = db.scalar(
                select(WhatsAppPresence).where(
                    WhatsAppPresence.user_id == admin.id
                )
            )
            assert row is not None
            assert row.status == "OFFLINE"
        finally:
            db.close()


def test_join_distributes_waiting_queue_to_online_group_member():
    password = "SenhaFila@2026"
    email = "fila-b2b@sulnet.local"
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            for presence in db.scalars(select(WhatsAppPresence)):
                presence.status = "OFFLINE"
                presence.last_left_at = utcnow()
            db.flush()
            user = db.scalar(select(User).where(User.email == email))
            if not user:
                user = User(
                    name="Operador Fila B2B",
                    email=email,
                    password_hash=hash_password(password),
                    role="CONSULTOR",
                    team="B2B",
                    active=True,
                    must_change_password=False,
                )
                db.add(user)
                db.flush()

            contact = Contact(
                phone="5555990000099",
                name="Cliente da Fila",
                business_name="Empresa da Fila",
            )
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                assigned_user_id=None,
                team="B2B",
                status="EM_FILA",
                opened_at=utcnow(),
            )
            db.add(conversation)
            db.commit()
            conversation_id = conversation.id
            user_id = user.id
        finally:
            db.close()

        token = _login(client, email, password)
        joined = client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        assert joined.status_code == 200
        assert joined.json()["online"] is True

        db = SessionLocal()
        try:
            conversation = db.get(Conversation, conversation_id)
            assert conversation is not None
            assert conversation.assigned_user_id == user_id
            assert conversation.status == "ATRIBUIDO"
        finally:
            db.close()


def test_consultant_inbox_api_stops_after_leaving_whatsapp():
    password = "SenhaAPI@2026"
    email = "api-presenca@sulnet.local"
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == email))
            if not user:
                user = User(
                    name="Consultor API",
                    email=email,
                    password_hash=hash_password(password),
                    role="CONSULTOR",
                    team="B2B",
                    active=True,
                    must_change_password=False,
                )
                db.add(user)
                db.commit()
        finally:
            db.close()

        token = _login(client, email, password)
        client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        )
        online = client.get("/api/conversations/inbox")
        assert online.status_code == 200

        left = client.post(
            "/api/whatsapp-presence/leave",
            data={"csrf_token": token},
        )
        assert left.status_code == 200

        offline = client.get("/api/conversations/inbox")
        assert offline.status_code == 403
