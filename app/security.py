from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User


PBKDF2_ITERATIONS = 390_000
_ARGON2 = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("A senha precisa ter pelo menos 12 caracteres.")
    return _ARGON2.hash(password)


def _verify_pbkdf2(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not encoded:
        return False
    if encoded.startswith("$argon2"):
        try:
            return bool(_ARGON2.verify(encoded, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    return _verify_pbkdf2(password, encoded)


def password_needs_rehash(encoded: str) -> bool:
    if not encoded:
        return True
    if not encoded.startswith("$argon2"):
        return True
    try:
        return _ARGON2.check_needs_rehash(encoded)
    except (InvalidHashError, VerificationError):
        return True


def login_user(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = int(user.session_version or 1)
    request.session["csrf_token"] = secrets.token_urlsafe(32)


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        user = db.get(User, int(user_id))
    except (TypeError, ValueError):
        request.session.clear()
        return None
    if not user or not user.active:
        request.session.clear()
        return None
    session_version = request.session.get("session_version")
    try:
        version_matches = (
            session_version is not None
            and int(session_version) == int(user.session_version or 1)
        )
    except (TypeError, ValueError):
        version_matches = False
    if not version_matches:
        request.session.clear()
        return None
    if (
        user.role.upper() in settings.required_mfa_roles
        and not user.mfa_enabled
        and request.url.path not in {
            "/mfa/setup",
            "/logout",
        }
    ):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/mfa/setup"},
        )
    if user.must_change_password and request.url.path not in {
        "/change-password",
        "/logout",
    }:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/change-password"},
        )
    return user


def require_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if not user or not user.active:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def require_roles(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso não autorizado.",
        )


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf_token")
    if (
        not expected
        or not submitted
        or not hmac.compare_digest(expected, submitted)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sessão inválida. Atualize a página.",
        )


def user_to_session_context(user: User | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "team": user.team,
        "branch": user.branch.name if user.branch else "Todas as unidades",
    }
