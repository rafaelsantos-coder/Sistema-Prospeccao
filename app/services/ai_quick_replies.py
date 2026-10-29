from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field


class QuickReply(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=20)


def _slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return (text or "opcao")[:64]


def normalize_quick_replies(items: list[QuickReply] | list[dict] | None) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (items or [])[:3]:
        if isinstance(raw, QuickReply):
            label = raw.label.strip()
            item_id = raw.id.strip() or _slug(label)
        elif isinstance(raw, dict):
            label = str(raw.get("label") or "").strip()
            item_id = str(raw.get("id") or "").strip() or _slug(label)
        else:
            continue
        if not label:
            continue
        label = label[:20]
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        clean.append({"id": item_id[:64], "label": label})
    return clean


def infer_quick_replies(reply_text: str) -> list[dict[str, str]]:
    """
    Camada de segurança para perguntas comerciais recorrentes.
    O modelo continua livre para gerar botões melhores; esta função só cobre
    perguntas previsíveis quando ele não retornar quick_replies.
    """
    text = (reply_text or "").strip().lower()
    if "?" not in text:
        return []

    if (
        ("casa" in text and "empresa" in text)
        or ("residencial" in text and "empresa" in text)
    ):
        return [
            {"id": "para_casa", "label": "Para casa"},
            {"id": "para_empresa", "label": "Para empresa"},
        ]

    if "800" in text and "650" in text:
        return [
            {"id": "plano_800", "label": "800 Mega"},
            {"id": "plano_650", "label": "650 Mega"},
        ]

    if all(term in text for term in ("preço", "velocidade")):
        options = [
            {"id": "preco", "label": "Preço"},
            {"id": "velocidade", "label": "Velocidade"},
        ]
        if "atendimento" in text or "qualidade" in text:
            options.append(
                {
                    "id": "atendimento" if "atendimento" in text else "qualidade",
                    "label": "Atendimento" if "atendimento" in text else "Qualidade",
                }
            )
        return options[:3]

    yes_no_cues = (
        "faz sentido",
        "você faria",
        "voce faria",
        "quer que eu",
        "posso te",
        "posso lhe",
        "gostaria de",
        "quer conhecer",
        "tem interesse",
        "vale a pena",
        "podemos avançar",
        "podemos avancar",
        "quer comparar",
    )
    if any(cue in text for cue in yes_no_cues):
        return [
            {"id": "sim", "label": "Sim"},
            {"id": "nao", "label": "Não"},
        ]

    return []


def quick_replies_for_message(
    reply_text: str,
    items: list[QuickReply] | list[dict] | None,
) -> list[dict[str, str]]:
    explicit = normalize_quick_replies(items)
    if len(explicit) >= 2:
        return explicit
    inferred = infer_quick_replies(reply_text)
    return inferred if len(inferred) >= 2 else []
