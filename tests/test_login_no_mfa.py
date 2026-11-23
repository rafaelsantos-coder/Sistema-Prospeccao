"""Regressões do login único por e-mail e senha, inclusive MFA legado ativo."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.security import hash_password, verify_password


EMAIL = "login.sem.codigo@sulnet.local"
PASSWORD = "MinhaSenhaExistente@2026"


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _prepare_user() -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        if user is None:
            user = User(
                name="Vendedor MFA Legado",
                email=EMAIL,
                password_hash=hash_password(PASSWORD),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
                mfa_enabled=True,
                mfa_secret_encrypted="legacy-encrypted-token",
            )
            db.add(user)
        else:
            user.password_hash = hash_password(PASSWORD)
            user.active = True
            user.must_change_password = False
            user.mfa_enabled = True
            user.mfa_secret_encrypted = "legacy-encrypted-token"
        db.commit()
        db.refresh(user)
        return user.id


def test_existing_password_logs_in_without_second_code():
    with TestClient(app) as client:
        user_id = _prepare_user()
        token = _csrf(client.get("/login").text)
        response = client.post(
            "/login",
            data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert client.get("/dashboard").status_code == 200

        with SessionLocal() as db:
            user = db.get(User, user_id)
            assert user is not None
            assert verify_password(PASSWORD, user.password_hash)
            assert user.mfa_enabled is True

        legacy_setup = client.get("/mfa/setup", follow_redirects=False)
        assert legacy_setup.status_code == 303
        assert legacy_setup.headers["location"] == "/dashboard"


def test_wrong_password_is_still_rejected_for_legacy_mfa_user():
    with TestClient(app) as client:
        _prepare_user()
        token = _csrf(client.get("/login").text)
        response = client.post(
            "/login",
            data={
                "email": EMAIL,
                "password": "SenhaIncorreta@2026",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert "E-mail ou senha inválidos" in response.text
        assert client.get("/dashboard", follow_redirects=False).status_code in (303, 401)


def test_legacy_code_verification_never_authenticates():
    with TestClient(app) as client:
        response = client.get("/mfa/verify", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert client.get("/dashboard", follow_redirects=False).status_code in (303, 401)
