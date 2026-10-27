from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.constants import COVERAGE_CITIES


@dataclass(frozen=True)
class ScoreResult:
    score: float
    temperature: str
    reasons: list[str]


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    # Números brasileiros locais têm 10 ou 11 dígitos. O DDD 55 não pode
    # ser confundido com o código do país.
    if len(digits) in {10, 11}:
        digits = "55" + digits
    elif len(digits) in {12, 13} and digits.startswith("55"):
        pass
    else:
        return None
    return digits


def temperature_from_score(score: float) -> str:
    if score >= 8:
        return "QUENTE"
    if score >= 5:
        return "MORNO"
    return "FRIO"


def calculate_sulnet_score(data: dict[str, Any]) -> ScoreResult:
    """Pontuação de 0 a 10 orientada à aderência comercial da Sulnet.

    O score do repositório original continua armazenado em ``legacy_score``.
    Esta regra evita tratar ausência de site ou nota baixa como intenção de compra.
    """

    score = 0.0
    reasons: list[str] = []

    phone = normalize_phone(data.get("phone"))
    if phone:
        score += 1.5
        reasons.append("telefone válido")

    email = (data.get("email") or "").strip()
    if email:
        score += 0.5
        reasons.append("e-mail disponível")

    city = (data.get("city") or data.get("location") or "").strip()
    if city in COVERAGE_CITIES or bool(data.get("coverage_confirmed")):
        score += 2.0
        reasons.append("cidade com cobertura")

    segment = (data.get("segment") or "").lower()
    b2b_keywords = {
        "empresa", "indústria", "industria", "clínica", "clinica", "mercado",
        "hotel", "escritório", "escritorio", "loja", "restaurante", "agro",
        "contabilidade", "farmácia", "farmacia", "academia", "escola",
    }
    if any(keyword in segment for keyword in b2b_keywords):
        score += 1.0
        reasons.append("segmento com aderência B2B")

    reviews = int(data.get("reviews_count") or data.get("reviewsCount") or 0)
    if reviews >= 20:
        score += 0.75
        reasons.append("empresa estabelecida")
    if reviews >= 100:
        score += 0.25

    website = (data.get("website") or "").strip()
    if website:
        score += 0.5
        reasons.append("presença digital validada")

    source = (data.get("source") or "").upper()
    if source == "TRAFEGO_PAGO":
        score += 2.0
        reasons.append("demanda originada por tráfego pago")
    elif source in {"INDICACAO", "BASE_ATIVA"}:
        score += 1.5
        reasons.append("origem com maior confiança")
    elif source == "APIFY":
        score += 0.5
        reasons.append("lead encontrado por prospecção ativa")

    if data.get("intent_signal"):
        score += 2.0
        reasons.append("sinal explícito de interesse")

    if data.get("existing_customer") is False:
        score += 0.5
        reasons.append("não identificado como cliente atual")

    if data.get("do_not_contact"):
        return ScoreResult(0.0, "FRIO", ["marcado como não contatar"])

    final_score = round(min(score, 10.0), 1)
    return ScoreResult(final_score, temperature_from_score(final_score), reasons)
