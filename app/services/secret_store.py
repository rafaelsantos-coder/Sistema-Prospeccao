from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretStoreError(RuntimeError):
    pass


def _fernet_for(value: str) -> Fernet:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _candidate_keys() -> list[tuple[str, str]]:
    values = [
        ("primary", settings.effective_data_encryption_key),
        ("legacy", settings.legacy_data_encryption_key),
        ("secret_key", settings.secret_key),
    ]
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, value in values:
        clean = (value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append((label, clean))
    return result


def encrypt_secret(value: str | None) -> str:
    clean = (value or "").strip()
    if not clean:
        return ""
    return _fernet_for(
        settings.effective_data_encryption_key
    ).encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_secret_with_source(value: str | None) -> tuple[str, str]:
    clean = (value or "").strip()
    if not clean:
        return "", "empty"

    for label, key in _candidate_keys():
        try:
            plain = _fernet_for(key).decrypt(
                clean.encode("ascii")
            ).decode("utf-8")
            return plain, label
        except (InvalidToken, ValueError, UnicodeError):
            continue

    raise SecretStoreError(
        "Nao foi possivel ler a credencial protegida."
    )


def decrypt_secret(value: str | None) -> str:
    plain, _source = decrypt_secret_with_source(value)
    return plain


def should_rotate_secret(value: str | None) -> bool:
    if not value:
        return False
    _plain, source = decrypt_secret_with_source(value)
    return source != "primary"
