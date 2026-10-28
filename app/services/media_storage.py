from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.client import Config
import httpx

from app.config import settings


class MediaStorageError(RuntimeError):
    pass


def _safe_filename(filename: str | None, mime_type: str | None = None) -> str:
    raw = Path(filename or "arquivo").name.strip() or "arquivo"
    raw = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw).strip(" .")
    if "." not in raw and mime_type:
        ext = mimetypes.guess_extension(mime_type.split(";")[0].strip()) or ""
        raw = raw + ext
    return raw[:180] or "arquivo"


class MediaStorage:
    def __init__(self) -> None:
        self.endpoint = settings.media_s3_endpoint
        self.bucket = settings.media_s3_bucket
        self.region = settings.media_s3_region or "auto"
        self.access_key = settings.media_s3_access_key_id
        self.secret_key = settings.media_s3_secret_access_key
        self.url_style = settings.media_s3_url_style or "virtual-host"
        self.max_bytes = max(1, settings.media_max_upload_mb) * 1024 * 1024

    @property
    def configured(self) -> bool:
        return bool(
            self.endpoint
            and self.bucket
            and self.access_key
            and self.secret_key
        )

    def _client(self):
        if not self.configured:
            raise MediaStorageError("O armazenamento de midia ainda nao foi configurado.")
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.url_style},
            ),
        )

    def build_key(
        self,
        *,
        conversation_id: int,
        filename: str | None,
        mime_type: str | None,
        direction: str,
    ) -> tuple[str, str]:
        safe_name = _safe_filename(filename, mime_type)
        now = datetime.now(timezone.utc)
        key = (
            f"conversations/{now:%Y/%m}/{conversation_id}/"
            f"{direction.lower()}-{uuid4().hex}-{safe_name}"
        )
        return key, safe_name

    def upload_bytes(
        self,
        *,
        data: bytes,
        conversation_id: int,
        filename: str | None,
        mime_type: str | None,
        direction: str = "OUTBOUND",
    ) -> tuple[str, str]:
        if not data:
            raise MediaStorageError("O arquivo esta vazio.")
        if len(data) > self.max_bytes:
            raise MediaStorageError(
                f"O arquivo excede o limite de {settings.media_max_upload_mb} MB."
            )
        key, safe_name = self.build_key(
            conversation_id=conversation_id,
            filename=filename,
            mime_type=mime_type,
            direction=direction,
        )
        extra: dict[str, str] = {
            "ContentType": mime_type or "application/octet-stream",
            "ContentDisposition": f'inline; filename="{safe_name}"',
        }
        try:
            self._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                **extra,
            )
        except Exception as exc:
            raise MediaStorageError("Nao foi possivel armazenar o arquivo.") from exc
        return self.storage_uri(key), safe_name

    def archive_remote(
        self,
        *,
        url: str,
        conversation_id: int,
        filename: str | None,
        mime_type: str | None,
        direction: str = "INBOUND",
    ) -> tuple[str, str]:
        if not self.configured:
            return url, _safe_filename(filename, mime_type)
        try:
            with httpx.Client(timeout=40.0, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                data = response.content
        except httpx.HTTPError as exc:
            raise MediaStorageError("Nao foi possivel arquivar a midia recebida.") from exc
        actual_mime = (
            mime_type
            or response.headers.get("content-type", "").split(";")[0].strip()
            or "application/octet-stream"
        )
        return self.upload_bytes(
            data=data,
            conversation_id=conversation_id,
            filename=filename,
            mime_type=actual_mime,
            direction=direction,
        )

    def storage_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key.lstrip('/')}"

    def key_from_uri(self, value: str) -> str | None:
        prefix = f"s3://{self.bucket}/"
        if value.startswith(prefix):
            return value[len(prefix) :]
        return None

    def presigned_download_url(
        self,
        value: str,
        *,
        filename: str | None = None,
        expires_seconds: int = 900,
    ) -> str:
        key = self.key_from_uri(value)
        if not key:
            if value.startswith("https://") or value.startswith("http://"):
                return value
            raise MediaStorageError("Midia armazenada em formato invalido.")
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            safe_name = _safe_filename(filename)
            params["ResponseContentDisposition"] = f'inline; filename="{safe_name}"'
        try:
            return self._client().generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=max(60, min(expires_seconds, 3600)),
            )
        except Exception as exc:
            raise MediaStorageError("Nao foi possivel gerar o acesso ao arquivo.") from exc
