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
    raw_text = (reply_text or "").strip().lower()
    if "?" not in raw_text:
        return []
    # Dado digitável (número a portar, e-mail, CEP...) nunca vira Sim/Não.
    if asks_for_free_text(reply_text):
        return []
    text = unicodedata.normalize("NFKD", raw_text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))

    if (
        ("telefonia movel" in text or "sulmovel" in text)
        and ("nova conexao" in text or "outro endereco" in text)
    ):
        return [
            {"id": "movel", "label": "Telefonia Móvel"},
            {"id": "nova_conexao", "label": "Nova conexão"},
        ]

    if "internet" in text and ("telefonia movel" in text or "sulmovel" in text):
        return [
            {"id": "internet", "label": "Internet"},
            {"id": "movel", "label": "Telefonia Móvel"},
        ]

    if (
        "ja e cliente sulnet" in text
        or "voce e cliente sulnet" in text
        or "voce ja e cliente sulnet" in text
        or "ja e nosso cliente" in text
        or "voce ja e nosso cliente" in text
    ):
        return [
            {"id": "sim", "label": "Sim"},
            {"id": "nao", "label": "Não"},
        ]

    if "fibra optica" in text and ("movel" in text or "sulmovel" in text):
        return [
            {"id": "fibra", "label": "Fibra óptica"},
            {"id": "movel", "label": "Móvel"},
        ]

    if all(value in text for value in ("200", "250", "650")) and (
        "corp" in text or "empresa" in text or "corporativo" in text
    ):
        return [
            {"id": "corp_200", "label": "Corp. 200"},
            {"id": "corp_250", "label": "Corp. 250"},
            {"id": "corp_650", "label": "Corp. 650"},
        ]

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

    if "principal problema" in text or "mais incomoda" in text or "o que incomoda" in text:
        return [
            {"id": "quedas", "label": "Internet cai"},
            {"id": "lentidao", "label": "Está lenta"},
            {"id": "preco", "label": "Preço alto"},
        ]

    if all(term in text for term in ("preco", "velocidade")):
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
        "voce faria",
        "quer que eu",
        "posso te",
        "posso lhe",
        "gostaria de",
        "quer conhecer",
        "tem interesse",
        "vale a pena",
        "podemos avancar",
        "quer comparar",
        "sim ou nao",
        "responda sim",
        "confirma que",
        # "deseja" sozinho é amplo demais ("Qual número você deseja portar?").
        "deseja unificar",
    )
    if any(cue in text for cue in yes_no_cues):
        return [
            {"id": "sim", "label": "Sim"},
            {"id": "nao", "label": "Não"},
        ]

    return []


_FREE_TEXT_FIELDS = (
    "cidade", "cep", "nome", "cpf", "cnpj", "e-mail", "email", "endereco", "rua",
    "bairro", "numero", "data de nascimento", "nascimento", "telefone", "complemento",
    "iccid", "ddd",
)
_FREE_TEXT_ASK = re.compile(
    r"\b(qual|quais|em qual|em que|me (?:passa|passe|informa|informe|diz|diga|envia|envie)"
    r"|pode me (?:passar|informar|dizer|enviar)|informe|digite)\b"
)


def asks_for_free_text(reply_text: str) -> bool:
    """A última pergunta pede um dado que o cliente precisa digitar (cidade, CEP...)."""
    text = unicodedata.normalize("NFKD", (reply_text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    if "?" not in text:
        return False
    question = re.split(r"[.!?\n]\s*", text[: text.rindex("?")])[-1]
    if " ou " in question:
        return False
    return bool(_FREE_TEXT_ASK.search(question)) and any(
        re.search(rf"\b{re.escape(field)}\b", question) for field in _FREE_TEXT_FIELDS
    )


def quick_replies_for_message(
    reply_text: str,
    items: list[QuickReply] | list[dict] | None,
) -> list[dict[str, str]]:
    if asks_for_free_text(reply_text):
        return []
    explicit = normalize_quick_replies(items)
    if len(explicit) >= 2:
        return explicit
    inferred = infer_quick_replies(reply_text)
    return inferred if len(inferred) >= 2 else []
