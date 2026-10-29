from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, User
from app.security import hash_password, verify_password


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/settings").text)


def _create_user(db, *, email: str, active: bool = True) -> User:
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        db.delete(existing)
        db.commit()
    user = User(
        name="Usuário Teste",
        email=email,
        password_hash=hash_password("SenhaOriginal@2026"),
        role="CONSULTOR",
        team="DIGITAL",
        active=active,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_admin_can_open_and_update_user():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            user = _create_user(db, email="edit.user@sulnet.local")
            user_id = user.id
        finally:
            db.close()

        page = client.get(f"/settings/users/{user_id}/edit")
        assert page.status_code == 200
        assert "Editar usuário" in page.text
        assert "Resetar senha" in page.text

        response = client.post(
            f"/settings/users/{user_id}/edit",
            data={
                "csrf_token": token,
                "name": "Usuário Atualizado",
                "email": "edit.user.updated@sulnet.local",
                "role": "SUPERVISOR",
                "team": "B2B",
                "branch_id": "",
                "active": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            updated = db.get(User, user_id)
            assert updated is not None
            assert updated.name == "Usuário Atualizado"
            assert updated.email == "edit.user.updated@sulnet.local"
            assert updated.role == "SUPERVISOR"
            assert updated.team == "B2B"
            assert updated.active is True
        finally:
            db.close()


def test_password_reset_generates_one_time_temporary_password():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            user = _create_user(db, email="reset.user@sulnet.local")
            user_id = user.id
            original_hash = user.password_hash
        finally:
            db.close()

        response = client.post(
            f"/settings/users/{user_id}/reset-password",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"

        match = re.search(
            r'id="temporary-password" value="([^"]+)"',
            response.text,
        )
        assert match
        temporary_password = match.group(1)
        assert len(temporary_password) == 16

        db = SessionLocal()
        try:
            updated = db.get(User, user_id)
            assert updated is not None
            assert updated.password_hash != original_hash
            assert verify_password(temporary_password, updated.password_hash)
            assert updated.must_change_password is True

            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "USER_PASSWORD_RESET",
                    AuditLog.entity_id == str(user_id),
                )
                .order_by(AuditLog.id.desc())
            )
            assert audit is not None
            assert temporary_password not in audit.payload_json
        finally:
            db.close()


def test_reset_does_not_reactivate_inactive_user():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            user = _create_user(
                db,
                email="inactive.reset.user@sulnet.local",
                active=False,
            )
            user_id = user.id
        finally:
            db.close()

        response = client.post(
            f"/settings/users/{user_id}/reset-password",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 200

        db = SessionLocal()
        try:
            updated = db.get(User, user_id)
            assert updated is not None
            assert updated.active is False
            assert updated.must_change_password is True
        finally:
            db.close()
