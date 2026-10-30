from __future__ import annotations

import re
from typing import Any


_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_CPF_RE = re.compile(r"(?<!\d)(\d{3})\.?\d{3}\.?\d{3}-?(\d{2})(?!\d)")
_CNPJ_RE = re.compile(r"(?<!\d)(\d{2})\.?\d{3}\.?\d{3}/?\d{4}-?(\d{2})(?!\d)")
_CEP_RE = re.compile(r"(?<!\d)(\d{5})-?(\d{3})(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}(?!\d)")
_BIRTH_RE = re.compile(r"(?<!\d)(\d{2})/(\d{2})/(\d{4})(?!\d)")

_SENSITIVE_KEYS = (
    "cpf",
    "cnpj",
    "document",
    "documento",
    "birth",
    "nascimento",
    "password",
    "senha",
    "token",
    "secret",
    "authorization",
    "bearer",
    "account",
    "conta",
    "agency",
    "agencia",
)
_MASKED_KEYS = (
    "email",
    "phone",
    "telefone",
    "celular",
    "cep",
)


def redact_pii_text(value: str | None) -> str:
    text = str(value or "")
    text = _EMAIL_RE.sub(lambda m: f"[EMAIL:{m.group(2)}]", text)
    text = _CNPJ_RE.sub("[CNPJ]", text)
    text = _CPF_RE.sub("[CPF]", text)
    text = _CEP_RE.sub("[CEP]", text)
    text = _PHONE_RE.sub("[TELEFONE]", text)
    text = _BIRTH_RE.sub("[DATA_NASCIMENTO]", text)
    return text


def sanitize_sensitive_value(key: str, value: Any) -> Any:
    folded = key.casefold()
    if any(token in folded for token in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if any(token in folded for token in _MASKED_KEYS):
        if value is None:
            return None
        return redact_pii_text(str(value))
    return sanitize_pii(value)


def sanitize_pii(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_sensitive_value(str(key), item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_pii(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_pii(item) for item in value]
    if isinstance(value, str):
        return redact_pii_text(value)
    return value
