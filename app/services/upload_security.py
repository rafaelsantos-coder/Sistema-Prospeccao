from __future__ import annotations

from pathlib import Path


class UploadValidationError(ValueError):
    pass


_ALLOWED: dict[str, set[str]] = {
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
    ".gif": {"image/gif"},
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    },
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/octet-stream",
    },
    ".txt": {"text/plain", "application/octet-stream"},
    ".csv": {"text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"},
    ".mp4": {"video/mp4", "application/octet-stream"},
    ".mov": {"video/quicktime", "video/mp4", "application/octet-stream"},
    ".m4v": {"video/x-m4v", "video/mp4", "application/octet-stream"},
    ".3gp": {"video/3gpp", "application/octet-stream"},
    ".mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    ".ogg": {"audio/ogg", "application/ogg", "application/octet-stream"},
    ".opus": {"audio/ogg", "audio/opus", "application/octet-stream"},
    ".wav": {"audio/wav", "audio/x-wav", "application/octet-stream"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "application/octet-stream"},
    ".aac": {"audio/aac", "audio/x-aac", "application/octet-stream"},
    ".amr": {"audio/amr", "audio/amr-wb", "application/octet-stream"},
}


def allowed_extensions() -> set[str]:
    return set(_ALLOWED)


def _signature_ok(extension: str, data: bytes) -> bool:
    head = data[:32]
    if extension in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if extension == ".pdf":
        return head.startswith(b"%PDF-")
    if extension in {".docx", ".xlsx", ".pptx"}:
        return head.startswith(b"PK\x03\x04")
    if extension in {".txt", ".csv"}:
        return b"\x00" not in data[:4096]
    if extension in {".mp4", ".mov", ".m4v", ".m4a"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if extension == ".3gp":
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if extension == ".mp3":
        return head.startswith(b"ID3") or (
            len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        )
    if extension in {".ogg", ".opus"}:
        return head.startswith(b"OggS")
    if extension == ".wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if extension == ".aac":
        return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0
    if extension == ".amr":
        return head.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    return False


def validate_upload(
    *,
    filename: str | None,
    mime_type: str | None,
    data: bytes,
) -> str:
    extension = Path(filename or "").suffix.lower()
    if extension not in _ALLOWED:
        raise UploadValidationError("Este tipo de arquivo não é permitido.")

    declared = (mime_type or "application/octet-stream").split(";")[0].strip().lower()
    if declared not in _ALLOWED[extension]:
        raise UploadValidationError(
            "O tipo real informado pelo arquivo não corresponde à extensão."
        )

    if not data:
        raise UploadValidationError("O arquivo está vazio.")
    if not _signature_ok(extension, data):
        raise UploadValidationError(
            "A assinatura do arquivo não corresponde ao formato permitido."
        )
    return extension
