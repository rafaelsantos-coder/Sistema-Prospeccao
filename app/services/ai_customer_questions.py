"""Interpretacao de duvidas tecnicas e contestacoes de localidade do cliente.

A ultima mensagem INBOUND e as correcoes explicitas do cliente prevalecem
sobre uma cidade herdada do CRM ou citada pelo atendente.
"""

from __future__ import annotations

import re
import unicodedata


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def denies_city(message: str | None, city: str | None) -> bool:
    """Uma negativa sobre morar ou instalar na cidade invalida a suposicao."""
    text, name = _fold(message), _fold(city)
    if not text or not name:
        return False
    return bool(re.search(
        rf"\b(?:nao sou (?:de|da|do|em)|nao moro (?:em|na|no)|"
        rf"nao estou (?:em|na|no)|nao (?:e|sera|vai ser) (?:em|de|para)) "
        rf"{re.escape(name)}\b",
        text,
    ))


def context_denies_city(context: dict, city: str | None) -> bool:
    """Considera somente falas do cliente, da mais nova para a mais antiga.

    Se o cliente confirmar novamente a cidade depois de negar, aceita a nova
    confirmacao. Frases OUTBOUND nunca comprovam localidade.
    """
    name = _fold(city)
    if not name:
        return False
    history = [
        str(item.get("body") or "")
        for item in context.get("history") or []
        if str(item.get("direction") or "").upper() == "INBOUND"
    ]
    latest = str(context.get("latest_customer_message") or "")
    if latest and (not history or _fold(history[-1]) != _fold(latest)):
        history.append(latest)
    for message in reversed(history):
        text = _fold(message)
        if denies_city(message, city):
            return True
        if re.search(
            rf"\b(?:sou de|moro em|moro na|moro no|"
            rf"instalacao em|instalar em|vai ser em|sera em|cidade de) "
            rf"{re.escape(name)}\b",
            text,
        ):
            return False
    return False


_HOME_CUES = re.compile(
    r"\b(?:toda a minha casa|toda minha casa|toda a casa|casa toda|"
    r"casa inteira|casa inteira|toda residencia|toda a residencia|"
    r"todos os comodos|todos os quartos|em todos os quartos|"
    r"cada comodo|apartamento inteiro|apartamento todo|"
    r"casa inteira|dentro da casa|no fundo da casa)\b"
)
_SIGNAL_CUES = re.compile(
    r"\b(?:funciona|funcionar|pega|pegar|alcanca|chega|chegar|"
    r"cobre|cobrir|sinal|wifi|wi fi|roteador|internet|cobertura)\b"
)
_ANSWER_REMINDER = re.compile(
    r"\b(?:quero (?:que )?(?:voce |me )?(?:responda|responder)|"
    r"(?:me )?responda o que pedi|nao respondeu|sem responder|"
    r"minha pergunta|o que perguntei|o que pedi)\b"
)


def is_home_wifi_question(value: str | None) -> bool:
    text = _fold(value)
    return bool(_HOME_CUES.search(text) and _SIGNAL_CUES.search(text))


def wants_prior_answer(value: str | None) -> bool:
    return bool(_ANSWER_REMINDER.search(_fold(value)))


def pending_home_wifi_question(context: dict) -> bool:
    """Recupera uma pergunta anterior ignorada quando o cliente cobra resposta."""
    latest = str(context.get("latest_customer_message") or "")
    if is_home_wifi_question(latest):
        return True
    if not wants_prior_answer(latest):
        return False
    # Procura a ultima pergunta ainda nao resolvida, sem aproveitar conversa
    # de outra pessoa, numero, instancia ou outra conversa.
    rows = list(context.get("history") or [])[-24:]
    for row in reversed(rows):
        body = str(row.get("body") or "")
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            answer = _fold(body)
            if (
                ("wi fi" in answer or "wifi" in answer)
                and any(token in answer for token in ("paredes", "alcance", "distancia", "comodos"))
            ):
                return False
        elif str(row.get("direction") or "").upper() == "INBOUND" and is_home_wifi_question(body):
            return True
    return False


def home_wifi_answer(context: dict) -> str | None:
    if not pending_home_wifi_question(context):
        return None
    latest = str(context.get("latest_customer_message") or "")
    prefix = ""
    if wants_prior_answer(latest):
        prefix = "Você tem razão. Desculpe por não responder sua pergunta antes. "
        if re.search(r"\bnao (?:sou|moro|estou)\b", _fold(latest)):
            prefix = "Desculpe por presumir sua cidade e por não responder sua pergunta. "
    floors_known = False
    area = ""
    previous_outbound = ""
    for row in (context.get("history") or [])[-40:]:
        body = str(row.get("body") or "")
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            previous_outbound = _fold(body)
            continue
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        folded = _fold(body)
        match = re.search(r"\b(\d{2,4})\s*(?:m2|metros quadrados)\b", folded)
        if match:
            area = match.group(1)
        if "mais de um" in folded and ("andares" in previous_outbound or "pisos" in previous_outbound):
            floors_known = True
    if area and floors_known:
        return (
            prefix
            + f"Em uma casa de {area} m² com mais de um andar, "
            "não dá para garantir que um único roteador cubra todos os cômodos: "
            "paredes e distância afetam o Wi-Fi, independentemente dos 800 Mega. "
            "Podemos avaliar a distribuição dos pontos de Wi-Fi antes de definir se precisa de outro roteador?"
        )
    return (
        prefix
        + "Sobre a cobertura dentro da casa: o Wi-Fi pode alcançar todos os cômodos, "
        "mas isso depende do tamanho da residência, das paredes e da posição do roteador; "
        "não dá para garantir sem avaliar. Sua casa tem um ou mais pisos?"
    )
