from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.client import Config
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import settings


LOG = logging.getLogger("sulnet_backup")
MAGIC = b"SULNETBK1"


def _pg_url() -> str:
    value = settings.database_url
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.media_s3_endpoint,
        region_name=settings.media_s3_region or "auto",
        aws_access_key_id=settings.media_s3_access_key_id,
        aws_secret_access_key=settings.media_s3_secret_access_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.media_s3_url_style},
        ),
    )


def _encryption_key() -> bytes:
    value = settings.backup_encryption_key
    if len(value) < 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY precisa ter ao menos 32 caracteres.")
    return hashlib.sha256(value.encode("utf-8")).digest()


def _encrypt_file(source: Path, target: Path) -> None:
    nonce = os.urandom(12)
    encryptor = Cipher(
        algorithms.AES(_encryption_key()),
        modes.GCM(nonce),
    ).encryptor()
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(MAGIC)
        dst.write(nonce)
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(encryptor.update(chunk))
        encryptor.finalize()
        dst.write(encryptor.tag)


def _decrypt_file(source: Path, target: Path) -> None:
    size = source.stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise RuntimeError("Backup criptografado inválido.")
    with source.open("rb") as src:
        if src.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("Cabeçalho de backup inválido.")
        nonce = src.read(12)
        src.seek(-16, os.SEEK_END)
        tag = src.read(16)
        ciphertext_end = size - 16
        src.seek(len(MAGIC) + 12)
        decryptor = Cipher(
            algorithms.AES(_encryption_key()),
            modes.GCM(nonce, tag),
        ).decryptor()
        remaining = ciphertext_end - (len(MAGIC) + 12)
        with target.open("wb") as dst:
            while remaining > 0:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("Backup truncado.")
                remaining -= len(chunk)
                dst.write(decryptor.update(chunk))
            decryptor.finalize()


def _verify_dump(path: Path) -> None:
    subprocess.run(
        ["pg_restore", "--list", str(path)],
        check=True,
        timeout=180,
        capture_output=True,
    )


def _retention(client) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(7, settings.backup_retention_days)
    )
    deleted = 0
    token = None
    while True:
        kwargs = {
            "Bucket": settings.media_s3_bucket,
            "Prefix": "backups/postgres/",
            "MaxKeys": 1000,
        }
        if token:
            kwargs["ContinuationToken"] = token
        result = client.list_objects_v2(**kwargs)
        old = [
            {"Key": item["Key"]}
            for item in result.get("Contents", [])
            if item.get("LastModified") and item["LastModified"] < cutoff
        ]
        if old:
            client.delete_objects(
                Bucket=settings.media_s3_bucket,
                Delete={"Objects": old, "Quiet": True},
            )
            deleted += len(old)
        if not result.get("IsTruncated"):
            break
        token = result.get("NextContinuationToken")
    return deleted


def create_backup() -> str:
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("Backup automático requer PostgreSQL.")
    if not all(
        [
            settings.media_s3_endpoint,
            settings.media_s3_bucket,
            settings.media_s3_access_key_id,
            settings.media_s3_secret_access_key,
        ]
    ):
        raise RuntimeError("Object storage não configurado.")

    now = datetime.now(timezone.utc)
    key = (
        f"backups/postgres/{now:%Y/%m/%d}/"
        f"sulnet-prospecta-{now:%Y%m%dT%H%M%SZ}.dump.enc"
    )

    with tempfile.TemporaryDirectory(prefix="sulnet-backup-") as tmp:
        dump = Path(tmp) / "database.dump"
        encrypted = Path(tmp) / "database.dump.enc"

        subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump),
                _pg_url(),
            ],
            check=True,
            timeout=1800,
            capture_output=True,
        )
        _verify_dump(dump)
        _encrypt_file(dump, encrypted)

        client = _client()
        client.upload_file(
            str(encrypted),
            settings.media_s3_bucket,
            key,
            ExtraArgs={
                "ContentType": "application/octet-stream",
                "Metadata": {
                    "format": "pg_dump_custom_aes256_gcm",
                    "created_at": now.isoformat(),
                },
            },
        )
        deleted = _retention(client)
        LOG.info(
            "backup_complete key=%s encrypted_bytes=%s retention_deleted=%s",
            key,
            encrypted.stat().st_size,
            deleted,
        )
    return key


def verify_latest_backup() -> str:
    client = _client()
    result = client.list_objects_v2(
        Bucket=settings.media_s3_bucket,
        Prefix="backups/postgres/",
        MaxKeys=1000,
    )
    items = sorted(
        result.get("Contents", []),
        key=lambda item: item.get("LastModified") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if not items:
        raise RuntimeError("Nenhum backup encontrado.")
    key = items[0]["Key"]

    with tempfile.TemporaryDirectory(prefix="sulnet-backup-verify-") as tmp:
        encrypted = Path(tmp) / "latest.enc"
        dump = Path(tmp) / "latest.dump"
        client.download_file(
            settings.media_s3_bucket,
            key,
            str(encrypted),
        )
        _decrypt_file(encrypted, dump)
        _verify_dump(dump)
    LOG.info("backup_verified key=%s", key)
    return key


def main() -> None:
    mode = os.getenv("BACKUP_MODE", "create").strip().casefold()
    if mode == "verify":
        verify_latest_backup()
    else:
        create_backup()


if __name__ == "__main__":
    main()
