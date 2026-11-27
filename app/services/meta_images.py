"""Persist official Meta inbound images and recover older callbacks securely."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Message, MessageEvent
from app.services.media_storage import MediaStorage
from app.services.meta_whatsapp import GRAPH_BASE, conversation_meta_account, service_for_meta_account
from app.services.upload_security import validate_upload

LOG = logging.getLogger(__name__)
_IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_ALLOWED_DOWNLOAD_HOSTS = {"lookaside.fbsbx.com", "lookaside.facebook.com"}


def image_ref(raw: str) -> str | None:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    image = payload.get("image") if isinstance(payload, dict) else None
    value = str(image.get("meta_media_id") or "") if isinstance(image, dict) else ""
    return value if re.fullmatch(r"[0-9]{1,64}", value) else None


def _refs(db: Session, messages) -> dict[int, str]:
    ids = [m.id for m in messages if m.direction == "INBOUND" and m.message_type == "IMAGE" and not m.media_url]
    if not ids:
        return {}
    rows = db.execute(
        select(MessageEvent.message_id, MessageEvent.payload_json)
        .where(MessageEvent.message_id.in_(ids), MessageEvent.event_type == "RECEIVED_CALLBACK")
        .order_by(MessageEvent.id.desc())
    )
    result = {}
    for message_id, raw in rows:
        if message_id not in result:
            ref = image_ref(raw)
            if ref:
                result[message_id] = ref
    return result


def meta_image_ids(db: Session, messages) -> set[int]:
    return set(_refs(db, messages))


def _allowed_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_DOWNLOAD_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def download_meta_image(service, media_id: str, max_bytes: int) -> tuple[bytes, str, str]:
    # Both requests require the account's token. Never expose it to the browser.
    if not re.fullmatch(r"[0-9]{1,64}", media_id):
        raise ValueError("invalid media ID")
    service._ensure_configured()
    headers = {"Authorization": f"Bearer {service.access_token}"}
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        response = client.get(
            f"{GRAPH_BASE}/{service.graph_api_version}/{media_id}",
            headers=headers,
            params={"phone_number_id": service.phone_number_id},
        )
        response.raise_for_status()
        info = response.json()
        url = str(info.get("url") or "")
        mime = str(info.get("mime_type") or "").split(";")[0].strip().lower()
        if not _allowed_url(url) or mime not in _IMAGE_EXTENSIONS:
            raise ValueError("invalid Meta image metadata")
        if int(info.get("file_size") or 0) > max_bytes:
            raise ValueError("image too large")
        with client.stream("GET", url, headers=headers) as download:
            download.raise_for_status()
            if int(download.headers.get("content-length") or 0) > max_bytes:
                raise ValueError("image too large")
            data = bytearray()
            for chunk in download.iter_bytes(65536):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("image too large")
    filename = "imagem" + _IMAGE_EXTENSIONS[mime]
    validate_upload(filename=filename, mime_type=mime, data=bytes(data))
    return bytes(data), mime, filename


def archive_meta_image(db: Session, message: Message) -> bool:
    if message.media_url:
        return True
    ref = _refs(db, [message]).get(message.id)
    if not ref:
        return False
    try:
        account = conversation_meta_account(db, message.conversation_id)
        if not account:
            return False
        storage = MediaStorage()
        if not storage.configured:
            LOG.warning("meta_image_storage_unconfigured message_id=%s", message.id)
            return False
        data, mime, filename = download_meta_image(service_for_meta_account(account), ref, storage.max_bytes)
        uri, _ = storage.upload_bytes(
            data=data, conversation_id=message.conversation_id,
            filename=filename, mime_type=mime, direction="INBOUND",
        )
        message.media_url = uri
        message.mime_type = mime
        return True
    except Exception as exc:
        # Only log the error class: URLs and provider responses may contain secrets.
        LOG.warning("meta_image_archive_failed message_id=%s error=%s", message.id, type(exc).__name__)
        return False


def archive_meta_image_task(message_id: int) -> None:
    with SessionLocal() as db:
        message = db.get(Message, message_id)
        if message and not message.media_url and archive_meta_image(db, message):
            db.commit()
