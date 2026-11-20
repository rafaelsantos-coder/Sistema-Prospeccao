"""Identifica retornos automáticos de contas comerciais do WhatsApp."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Message

_STRONG_CUES = re.compile(
    r"estamos fechad|fora do (nosso )?horario|horarios? de (funcionamento|atendimento)"
    r"|no momento,? nao (estamos|podemos|conseguimos)|nao estamos disponiveis"
    r"|(agradece|agradecemos) (o |pelo |seu )?contato|retornaremos (o |seu )?contato"
    r"|em breve (retornaremos|responderemos|entraremos|daremos)|assim que possivel,? (retornaremos|responderemos)"
    r"|mensagem automatica|resposta automatica|seja bem[- ]?vind|que bom receber (voce|vc)"
    r"|aqui e (o|a) [a-z]+,? (assistente|atendente virtual)|assistente virtual"
    r"|digite (o numero|a opcao|[0-9])|escolha uma (das )?opc|responda com o numero"
    r"|faca (o )?seu pedido|agende (o )?seu pedido|cardapio|nosso site:|acesse nosso"
)
_SOFT_CUE = re.compile(r"como (podemos|posso) (te |lhe )?ajudar")
_MENU_RETURN = re.compile(
    r"(?:opcao (?:de menu )?invalida|(?:o |seu )?atendimento (?:foi )?(?:finalizado|encerrado))"
    r"[.! ]*(?:por favor,? )?(?:tente novamente[.! ]*)?$"
)
_MENU_INSTRUCTIONS = re.compile(r"(?:digite|informe) o numero correspondente|escolha .{1,70} digitando (?:apenas )?o numero")
_HUMAN_QUESTION = re.compile(
    r"^(?:(?:oi|ola|bom dia|boa tarde|boa noite)[,!. ]+)?"
    r"(?:qual (?:e )?(?:o |seu |vosso )?horario|quais (?:sao )?(?:os )?horarios|"
    r"recebi (?:uma |a )?mensagem automatica|nao quero receber)"
)


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def is_business_auto_reply(text: str | None) -> bool:
    folded = _fold(text).replace("*", "").replace("_", "")
    if not folded or folded.startswith("[audio"):
        return False
    if _HUMAN_QUESTION.search(folded):
        return False
    if _MENU_RETURN.fullmatch(folded) or _MENU_INSTRUCTIONS.search(folded):
        return True
    if _STRONG_CUES.search(folded):
        return True
    return len(folded) > 90 and bool(_SOFT_CUE.search(folded))


def latest_inbound_is_auto_reply(db: Session, conversation_id: int) -> bool:
    message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == "INBOUND")
        .order_by(Message.id.desc())
        .limit(1)
    )
    return bool(message and is_automatic_inbound(message))


def is_automatic_inbound(message: Message) -> bool:
    return (
        message.direction == "INBOUND"
        and message.message_type in {"TEXT", "BUTTONS"}
        and is_business_auto_reply(message.body)
    )
