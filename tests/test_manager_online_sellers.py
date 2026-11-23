from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import User, WhatsAppPresence
from app.security import hash_password
from app.services.whatsapp_presence import join_whatsapp
from app.timeutils import utcnow


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_online_sellers_is_restricted_to_managers_and_uses_live_whatsapp_presence():
    password = "SenhaTeste@2026"
    emails = [
        "teste-painel-online@sulnet.local",
        "teste-painel-expirado@sulnet.local",
        "teste-painel-inativo@sulnet.local",
    ]
    user_ids = []
    try:
        with TestClient(app) as client:
            # Autenticação obrigatória.
            assert client.get("/api/whatsapp-presence/online-sellers").status_code == 401

            db = SessionLocal()
            try:
                for email in emails:
                    existing = db.scalar(select(User).where(User.email == email))
                    if existing:
                        db.query(WhatsAppPresence).filter(WhatsAppPresence.user_id == existing.id).delete()
                        db.delete(existing)
                        db.flush()
                for index, email in enumerate(emails):
                    user = User(
                        name=["Teste Online", "Teste Expirado", "Teste Inativo"][index],
                        email=email,
                        password_hash=hash_password(password),
                        role="CONSULTOR",
                        team="LOJA",
                        active=index != 2,
                        must_change_password=False,
                    )
                    db.add(user)
                    db.flush()
                    user_ids.append(user.id)
                    row = join_whatsapp(db, user)
                    if index == 1:
                        row.last_heartbeat_at = utcnow() - timedelta(minutes=5)
                db.commit()
            finally:
                db.close()

            _login(client, "admin@sulnet.local", "Sulnet@2026!")
            page = client.get("/conversations")
            assert page.status_code == 200
            assert 'data-online-sellers' in page.text
            endpoint = client.get("/api/whatsapp-presence/online-sellers")
            assert endpoint.status_code == 200
            assert endpoint.headers["cache-control"] == "private, no-store"
            sellers = endpoint.json()["sellers"]
            assert any(item["name"] == "Teste Online" for item in sellers)
            assert not any(item["name"] == "Teste Expirado" for item in sellers)
            assert not any(item["name"] == "Teste Inativo" for item in sellers)
            assert endpoint.json()["count"] == len(sellers)

        with TestClient(app) as consultant:
            _login(consultant, emails[0], password)
            assert consultant.get("/api/whatsapp-presence/online-sellers").status_code == 403
            assert 'data-online-sellers' not in consultant.get("/dashboard").text
    finally:
        db = SessionLocal()
        try:
            if user_ids:
                db.query(WhatsAppPresence).filter(WhatsAppPresence.user_id.in_(user_ids)).delete(synchronize_session=False)
                db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
