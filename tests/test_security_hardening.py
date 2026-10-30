from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest

from app.security import hash_password, password_needs_rehash, verify_password
from app.services import network_security, secret_store
from app.services.network_security import RemoteURLValidationError, validate_remote_url
from app.services.upload_security import UploadValidationError, validate_upload


def _legacy_hash(password: str) -> str:
    salt = b"0123456789abcdef"
    iterations = 390_000
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return "pbkdf2_sha256$" + str(iterations) + "$" + salt_b64 + "$" + digest_b64


def test_passwords_use_argon2id_and_accept_legacy_pbkdf2():
    encoded = hash_password("SenhaSegura@2026")
    assert encoded.startswith("$argon2id$")
    assert verify_password("SenhaSegura@2026", encoded)
    assert not verify_password("SenhaErrada@2026", encoded)
    assert password_needs_rehash(encoded) is False

    legacy = _legacy_hash("SenhaAntiga@2026")
    assert verify_password("SenhaAntiga@2026", legacy)
    assert password_needs_rehash(legacy) is True


def test_secret_store_reads_legacy_and_writes_primary(monkeypatch):
    custom_settings = SimpleNamespace(
        effective_data_encryption_key="new-data-key-" + "x" * 32,
        legacy_data_encryption_key="old-data-key-" + "y" * 32,
        secret_key="legacy-secret-" + "z" * 32,
    )
    monkeypatch.setattr(secret_store, "settings", custom_settings)

    legacy_cipher = secret_store._fernet_for(
        custom_settings.legacy_data_encryption_key
    ).encrypt(b"segredo-antigo").decode("ascii")

    plain, source = secret_store.decrypt_secret_with_source(legacy_cipher)
    assert plain == "segredo-antigo"
    assert source == "legacy"
    assert secret_store.should_rotate_secret(legacy_cipher)

    new_cipher = secret_store.encrypt_secret("segredo-novo")
    plain, source = secret_store.decrypt_secret_with_source(new_cipher)
    assert plain == "segredo-novo"
    assert source == "primary"


def test_upload_validation_uses_allowlist_and_signature():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    assert validate_upload(
        filename="foto.png",
        mime_type="image/png",
        data=png,
    ) == ".png"

    with pytest.raises(UploadValidationError):
        validate_upload(
            filename="malware.exe",
            mime_type="application/octet-stream",
            data=b"MZ" + b"x" * 20,
        )

    with pytest.raises(UploadValidationError):
        validate_upload(
            filename="falso.png",
            mime_type="image/png",
            data=b"MZ" + b"x" * 20,
        )


def test_remote_url_rejects_private_network(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "settings",
        SimpleNamespace(
            is_production=True,
            media_remote_allowed_hosts="",
        ),
    )
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(RemoteURLValidationError):
        validate_remote_url("https://example.com/arquivo.jpg")


def test_remote_url_accepts_public_https(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "settings",
        SimpleNamespace(
            is_production=True,
            media_remote_allowed_hosts="*.example.com",
        ),
    )
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert (
        validate_remote_url("https://cdn.example.com/arquivo.jpg")
        == "https://cdn.example.com/arquivo.jpg"
    )
