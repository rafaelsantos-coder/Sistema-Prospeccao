from __future__ import annotations

import importlib
from typing import Any

from app.services.scoring import normalize_phone


def legacy_modules_available() -> bool:
    try:
        importlib.import_module("scripts.apify_scraper")
        importlib.import_module("scripts.copy_generator")
        importlib.import_module("scripts.rate_limiter")
        return True
    except Exception:
        return False


def legacy_score(item: dict[str, Any]) -> float | None:
    try:
        module = importlib.import_module("scripts.apify_scraper")
        return float(module.score_lead(item))
    except Exception:
        return None


def generate_message(segment: str, day: int, channel: str, lead_data: dict[str, Any]) -> str:
    try:
        module = importlib.import_module("scripts.copy_generator")
        return str(module.get_message(segment, day, channel.lower(), lead_data))
    except Exception:
        name = lead_data.get("contact_name") or lead_data.get("business_name") or "tudo bem"
        if channel.upper() == "EMAIL":
            return (
                f"Olá, {name}. Sou da Sulnet e gostaria de entender como está a conectividade "
                "da sua empresa. Podemos conversar sobre uma solução adequada à sua operação?"
            )
        return (
            f"Olá, {name}! Tudo bem? Sou da Sulnet. Estamos conversando com empresas de "
            f"{segment} da região para entender necessidades de internet e telefonia. "
            "Posso fazer duas perguntas rápidas?"
        )


def rate_status() -> dict[str, Any]:
    try:
        module = importlib.import_module("scripts.rate_limiter")
        return module.get_status()
    except Exception:
        return {
            "channels": {
                "whatsapp": {"limit": 30, "sent_today": 0, "remaining": 30},
                "email": {"limit": 200, "sent_today": 0, "remaining": 200},
            },
            "source": "fallback",
        }


def normalize_legacy_phone(raw: str | None) -> str | None:
    try:
        module = importlib.import_module("scripts.apify_scraper")
        return module.normalize_phone(raw or "")
    except Exception:
        return normalize_phone(raw)
