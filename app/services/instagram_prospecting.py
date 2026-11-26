"""Dry-run rules for Instagram comment prospecting. Never sends messages."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable, Literal

Mode = Literal["commercial", "all"]

MESSAGES = {
    "price": "Olá! Obrigado pelo interesse na Sulnet. Em qual cidade você gostaria de contratar internet?",
    "coverage": "Olá! Podemos consultar a cobertura da Sulnet. Qual é a sua cidade?",
    "interest": "Olá! Que bom ter você por aqui. Gostaria de conhecer os planos de internet da Sulnet?",
    "generic": "Olá! Obrigado por interagir com a Sulnet. Podemos ajudar com alguma dúvida sobre nossos serviços?",
    "praise": "Muito obrigado pelo carinho e por acompanhar a Sulnet!",
}


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(c for c in ascii_text if not unicodedata.combining(c))
    return " " + re.sub(r"[^a-z0-9]+", " ", without_marks).strip() + " "


def _matches(text: str, phrases: tuple[str, ...]) -> bool:
    return any(" " + phrase + " " in text for phrase in phrases)


def classify_comment(text: str, mode: Mode = "commercial") -> str:
    """Conservative deterministic triage; support/opt-out always beat sales."""
    normalized = _normalize(text)
    if not normalized.strip():
        return "manual_review"
    if _matches(normalized, (
        "nao tenho interesse", "nao quero", "sem interesse", "parem",
        "nao me chame", "nao me mandem", "nao me mande", "nao obrigado",
        "nao tenho vontade", "spam", "nao entre em contato",
    )):
        return "opt_out"
    if _matches(normalized, (
        "sem internet", "sem conexao", "sem sinal", "nao funciona",
        "nao resolve", "reclamacao", "reclamar", "cancelar", "cancelamento",
        "pessimo", "pessima", "horrivel", "ruim", "problema", "lenta",
        "lento", "instavel", "caiu", "suporte", "fatura", "boleto", "nao recomendo",
        "nao atende", "nao consigo", "aumento", "cobranca", "caro", "cara",
        "nao tem cobertura",
    )):
        return "support_review"
    if _matches(normalized, ("cobertura", "viabilidade", "atende", "disponibilidade", "tem fibra")):
        return "coverage"
    if _matches(normalized, ("valor", "preco", "quanto custa", "mensalidade", "quanto fica")):
        return "price"
    if _matches(normalized, ("quero contratar", "tenho interesse", "quero saber", "quero internet",
                             "quero", "planos", "plano", "oferta", "promocao", "interesse")):
        return "interest"
    if _matches(normalized, ("parabens", "excelente", "maravilhoso", "maravilhosa", "adorei", "recomendo")):
        return "praise"
    # Unknowns are only suggested in the explicit broad preview mode.
    # Mentions-only, emojis-only, or empty comments never get a draft.
    if re.fullmatch(r"(?:\s*@[\w.]+\s*)+", text.strip()) or len(normalized.strip()) < 3:
        return "manual_review"
    return "generic" if mode == "all" else "manual_review"


def _identifier(value: object) -> str | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < len(value) <= 80:
        return value
    return None


def preview_comments(
    payloads: Iterable[object],
    account_id: str,
    mode: Mode = "commercial",
    media_id: str | None = None,
) -> dict:
    """Aggregate stored comment changes, not customer data or send eligibility.

    A stored receipt timestamp cannot establish a comment's creation time.
    No private reply eligibility or authorization is inferred from this preview.
    """
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for event in payloads:
        if not isinstance(event, dict) or event.get("field") != "comments":
            counts["ignored"] += 1
            continue
        value = event.get("value")
        if not isinstance(value, dict):
            counts["invalid"] += 1
            continue
        author = value.get("from")
        media = value.get("media")
        if not isinstance(author, dict) or not isinstance(media, dict):
            counts["invalid"] += 1
            continue
        author_id = _identifier(author.get("id"))
        comment_id = _identifier(value.get("id"))
        post_id = _identifier(media.get("id"))
        comment = value.get("text")
        if not all((author_id, comment_id, post_id)) or not isinstance(comment, str):
            counts["invalid"] += 1
            continue
        if media_id is not None and post_id != media_id:
            counts["filtered"] += 1
            continue
        if comment_id in seen:
            counts["duplicate"] += 1
            continue
        seen.add(comment_id)
        if author_id == account_id or value.get("is_self") is True:
            counts["self"] += 1
            continue
        if str(media.get("media_product_type", "")).upper() == "LIVE":
            counts["manual_review"] += 1
            continue
        counts[classify_comment(comment, mode)] += 1
    return {
        "mode": mode,
        "simulation_only": True,
        "send_enabled": False,
        "counts": dict(sorted(counts.items())),
        "draft_templates": {key: MESSAGES[key] for key in MESSAGES if counts[key]},
    }
