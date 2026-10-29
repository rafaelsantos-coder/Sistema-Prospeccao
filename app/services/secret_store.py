from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretStoreError(RuntimeError):
    pass


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(value: str | None) -> str:
    clean = (value or "").strip()
    if not clean:
        return ""
    return _fernet().encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    clean = (value or "").strip()
    if not clean:
        return ""
    try:
        return _fernet().decrypt(clean.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise SecretStoreError("Nao foi possivel ler a credencial protegida.") from exc
