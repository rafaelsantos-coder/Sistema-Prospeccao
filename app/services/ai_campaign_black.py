"""Campanha "Black da Sulnet" (a Black que pula o fim de ano).

Regras (definidas pelo comercial):
- vale para clientes da base e clientes novos;
- só móvel: 1ª mensalidade em 10/01/2027;
- combo (fibra óptica + qualquer plano móvel): 1ª mensalidade dos dois em 10/01/2027;
- combo incompleto perde a promoção; fidelidade e instalação seguem como hoje;
- cliente novo passa pela análise de crédito normal;
- no CRM a venda leva a tag "Promoção Black 2027" (combo = duas vendas separadas).

Os textos são montados aqui, em código, para a IA nunca inventar condição.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

CAMPAIGN_TAG = "Promoção Black 2027"
FIRST_DUE_DATE = "10/01/2027"
VALID_UNTIL_LABEL = "30/11/2026"
# 30/11/2026 23:59:59 em Brasília (UTC-3)
_VALID_UNTIL = datetime(2026, 12, 1, 3, 0, 0, tzinfo=timezone.utc) - timedelta(seconds=1)

OFFER_QUESTION = "Quer conhecer nossa promoção Black da Sulnet?"
OFFER_MARK = "promocao black da sulnet"
OPTIONS_MARK = "black que pula o fim de ano"

OPTIONS_TEXT = (
    "Black da Sulnet: a Black que pula o fim de ano! 🎉\n"
    f"• Telefonia Móvel: você só paga a 1ª mensalidade em {FIRST_DUE_DATE}.\n"
    f"• Combo (fibra óptica + qualquer plano móvel): a 1ª mensalidade dos dois só vence em {FIRST_DUE_DATE}. "
    "Para valer, é preciso contratar a internet e o móvel.\n"
    f"Vale para contratações até {VALID_UNTIL_LABEL}. Qual você prefere?"
)

SUMMARY_MARKER = f"[{CAMPAIGN_TAG}]"


def black_active(now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current <= _VALID_UNTIL


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def is_offer_question(previous_outbound: str | None) -> bool:
    return OFFER_MARK in _fold(previous_outbound)


def is_options_question(previous_outbound: str | None) -> bool:
    return OPTIONS_MARK in _fold(previous_outbound)


def was_offered(history: list[dict] | None) -> bool:
    return any(
        str(row.get("direction") or "").upper() == "OUTBOUND"
        and (OFFER_MARK in _fold(row.get("body")) or OPTIONS_MARK in _fold(row.get("body")))
        for row in history or []
    )


_YES = {"sim", "quero", "quero sim", "claro", "pode", "pode ser", "pode sim", "bora", "ok", "s", "black",
        "quero conhecer", "quero conhecer a promocao"}
_NO = {"nao", "n", "agora nao", "nao quero", "nao obrigado", "nao precisa", "depois"}


def answer_to_offer(text: str | None) -> str:
    folded = _fold(text)
    if folded in _YES or folded.startswith(("sim ", "quero ")):
        return "YES"
    if folded in _NO or folded.startswith(("nao ", "agora nao")):
        return "NO"
    return ""


def choice_from_options(text: str | None) -> str:
    """MOBILE, COMBO ou vazio."""
    folded = _fold(text)
    if not folded:
        return ""
    if "combo" in folded or ("fibra" in folded and "movel" in folded) or ("internet" in folded and "movel" in folded):
        return "COMBO"
    if folded in {"so movel", "movel", "telefonia movel", "so o movel", "somente movel", "celular"}:
        return "MOBILE"
    return ""


def black_accepted(history: list[dict] | None) -> bool:
    """O cliente já respondeu "sim" à pergunta da Black nesta conversa."""
    rows = list(history or [])
    for index, row in enumerate(rows):
        if str(row.get("direction") or "").upper() != "OUTBOUND":
            continue
        if not (is_offer_question(row.get("body")) or is_options_question(row.get("body"))):
            continue
        for reply in rows[index + 1:]:
            if str(reply.get("direction") or "").upper() == "INBOUND":
                if is_options_question(row.get("body")) or answer_to_offer(reply.get("body")) == "YES":
                    return True
                break
    return False


def with_marker(summary: str | None) -> str:
    text = (summary or "").strip()
    if SUMMARY_MARKER in text:
        return text
    return f"{text} {SUMMARY_MARKER}".strip()


def has_marker(summary: str | None) -> bool:
    return SUMMARY_MARKER in (summary or "")


FIRST_DUE_ISO = "2027-01-10"
COMBO_STAGE_FIBER = "FIBER"
COMBO_STAGE_MOBILE = "MOBILE"
COMBO_STAGE_DECLINED = "DECLINED"


def black_choice(history: list[dict] | None) -> str:
    """COMBO ou MOBILE, conforme a resposta do cliente às opções da Black."""
    rows = list(history or [])
    for index, row in enumerate(rows):
        if str(row.get("direction") or "").upper() == "OUTBOUND" and is_options_question(row.get("body")):
            for reply in rows[index + 1:]:
                if str(reply.get("direction") or "").upper() == "INBOUND":
                    return choice_from_options(reply.get("body"))
    return ""


MOBILE_CHOICES = (
    {"gb": 5, "name": "Sulmóvel 5GB", "price": 44.90},
    {"gb": 7, "name": "Sulmóvel 7GB", "price": 47.90},
    {"gb": 10, "name": "Sulmóvel 10GB", "price": 53.90},
    {"gb": 15, "name": "Sulmóvel 15GB", "price": 69.80},
)


def mobile_choice_from_text(text: str | None) -> dict | None:
    match = re.search(r"\b(5|7|10|15)\s*(?:gb|giga|gigas|g)?\b", _fold(text))
    if not match:
        return None
    gb = int(match.group(1))
    return next((item for item in MOBILE_CHOICES if item["gb"] == gb), None)


def _brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def combo_mobile_prompt() -> str:
    lines = [
        "Contrato da internet assinado! ✅",
        f"Agora falta o seu plano móvel para garantir a Black (1ª mensalidade dos dois só em {FIRST_DUE_DATE}):",
    ]
    lines += [f"• {item['gb']}GB: R$ {_brl(item['price'])}/mês" for item in MOBILE_CHOICES]
    lines.append("Qual plano móvel você quer?")
    return "\n".join(lines)


def combo_cpf_prompt(plan_name: str, price: float) -> str:
    return (
        f"Anotado: {plan_name} por R$ {_brl(price)}/mês. Como o móvel é uma contratação separada, "
        "preciso consultar o seu CPF de novo. Pode me enviar?"
    )


COMBO_DECLINED_MESSAGE = (
    "Tudo bem! Sua internet está contratada. A promoção Black só vale com internet e móvel juntos, "
    "então ela não será aplicada. Nossa equipe vai entrar em contato para agendar a ativação do seu serviço."
)
FIXED_PREFIXES = (
    "Contrato da internet assinado! ✅",
    "Anotado: Sulmóvel",
    "Tudo bem! Sua internet está contratada.",
)
