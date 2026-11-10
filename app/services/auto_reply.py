"""Resposta automática de empresa (WhatsApp Business) não é o cliente falando.

Conversas de 10/11: "Olá! Estamos fechados no momento! Horários de
funcionamento..." recebeu "Qual seu CPF?", e o assistente automático de uma
clínica recebeu cinco perguntas seguidas da IA. Quando só o robô da empresa
respondeu, a IA manda uma única mensagem para o responsável e para.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Message

_STRONG_CUES = re.compile(
    r"estamos fechad|fora do (nosso )?horario|horarios? de (funcionamento|atendimento)"
    r"|no momento,? nao (estamos|podemos|conseguimos)|nao estamos disponiveis"
    r"|(agradece|agradecemos) (o |pelo |seu |o seu )*contato|retornaremos (o |seu |o seu )*contato"
    r"|em breve (retornaremos|responderemos|entraremos|daremos)|assim que possivel,? (retornaremos|responderemos)"
    r"|mensagem automatica|resposta automatica|seja bem[- ]?vind|que bom receber (voce|vc)"
    r"|aqui e (o|a) [a-z]+,? (assistente|atendente virtual)|assistente virtual"
    r"|digite (o numero|a opcao|[0-9])|escolha uma (das )?opc|responda com o numero"
    r"|faca (o )?seu pedido|agende (o )?seu pedido|cardapio|nosso site:|acesse nosso"
)
_SOFT_CUE = re.compile(r"como (podemos|posso) (te |lhe )?ajudar")


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def is_business_auto_reply(text: str | None) -> bool:
    folded = _fold(text)
    if not folded:
        return False
    if _STRONG_CUES.search(folded):
        return True
    return len(folded) > 90 and bool(_SOFT_CUE.search(folded))


def _inbound_bodies(db: Session, conversation_id: int) -> list[tuple[int, str]]:
    rows = db.execute(
        select(Message.id, Message.body)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
            Message.message_type.in_(["TEXT", "BUTTONS"]),
        )
        .order_by(Message.id)
    ).all()
    return [(row[0], str(row[1] or "")) for row in rows if str(row[1] or "").strip()]


def only_auto_replies(db: Session, conversation_id: int) -> bool:
    """Todo texto recebido nesta conversa é resposta automática de empresa."""
    bodies = _inbound_bodies(db, conversation_id)
    return bool(bodies) and all(is_business_auto_reply(body) for _, body in bodies)


def ai_already_spoke_after_auto_reply(db: Session, conversation_id: int) -> bool:
    bodies = _inbound_bodies(db, conversation_id)
    if not bodies:
        return False
    first_id = bodies[0][0]
    return bool(
        db.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == "OUTBOUND",
                Message.id > first_id,
            )
            .limit(1)
        )
    )


def latest_inbound_is_auto_reply(db: Session, conversation_id: int) -> bool:
    bodies = _inbound_bodies(db, conversation_id)
    return bool(bodies) and is_business_auto_reply(bodies[-1][1])
