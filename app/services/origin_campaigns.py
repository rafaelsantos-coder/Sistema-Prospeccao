"""Campanhas de anúncio: texto padrão da mensagem → origem enviada ao CRM."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Message, OriginCampaign

DEFAULT_CRM_ORIGIN = "Sulnet Prospecta"
MIN_MATCH_CHARS = 12
MAX_INBOUND_SCAN = 12


@dataclass(frozen=True)
class OriginMatch:
    campaign_id: int
    campaign_name: str
    crm_origin: str
    match_text: str


def fold_match_text(value: str | None) -> str:
    """Normaliza para comparação: sem acento, minúsculo, pontuação → espaço."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def validate_campaign_fields(
    *,
    name: str,
    match_text: str,
    crm_origin: str,
    link: str = "",
) -> tuple[str, str, str, str]:
    clean_name = " ".join((name or "").strip().split())
    clean_match = " ".join((match_text or "").strip().split())
    clean_origin = " ".join((crm_origin or "").strip().split())
    clean_link = (link or "").strip()[:500]
    if len(clean_name) < 2 or len(clean_name) > 180:
        raise ValueError("Informe o nome da campanha (2 a 180 caracteres).")
    if len(clean_match) < MIN_MATCH_CHARS or len(clean_match) > 2000:
        raise ValueError(
            f"Informe o texto padrão da mensagem (mínimo {MIN_MATCH_CHARS} caracteres)."
        )
    if len(fold_match_text(clean_match)) < MIN_MATCH_CHARS:
        raise ValueError(
            "O texto padrão precisa ter conteúdo suficiente para identificar a campanha."
        )
    if len(clean_origin) < 2 or len(clean_origin) > 180:
        raise ValueError("Informe a origem do CRM (2 a 180 caracteres).")
    if clean_link and not (
        clean_link.startswith("http://") or clean_link.startswith("https://")
    ):
        raise ValueError("O link da campanha deve começar com http:// ou https://.")
    return clean_name, clean_match, clean_origin, clean_link


def list_active_campaigns(db: Session) -> list[OriginCampaign]:
    return list(
        db.scalars(
            select(OriginCampaign)
            .where(OriginCampaign.active.is_(True))
            .order_by(OriginCampaign.name)
        )
    )


def list_campaigns(db: Session, *, include_inactive: bool = True) -> list[OriginCampaign]:
    query = select(OriginCampaign).order_by(
        OriginCampaign.active.desc(), OriginCampaign.name
    )
    if not include_inactive:
        query = query.where(OriginCampaign.active.is_(True))
    return list(db.scalars(query))


def inbound_bodies_for_match(db: Session, conversation_id: int) -> list[str]:
    """Primeiras mensagens inbound de texto — onde costuma vir o texto do anúncio."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
            Message.message_type.in_(("TEXT", "BUTTON", "LIST")),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(MAX_INBOUND_SCAN)
    )
    return [row.body or "" for row in rows if (row.body or "").strip()]


def match_origin_from_texts(
    campaigns: list[OriginCampaign],
    message_bodies: list[str],
) -> OriginMatch | None:
    """Escolhe a campanha cujo texto padrão está contido na mensagem (mais longo vence)."""
    if not campaigns or not message_bodies:
        return None
    folded_bodies = [fold_match_text(body) for body in message_bodies]
    folded_bodies = [body for body in folded_bodies if body]
    if not folded_bodies:
        return None

    best: OriginMatch | None = None
    best_len = -1
    for campaign in campaigns:
        if not campaign.active:
            continue
        needle = fold_match_text(campaign.match_text)
        if len(needle) < MIN_MATCH_CHARS:
            continue
        if not any(needle in body for body in folded_bodies):
            continue
        if len(needle) > best_len:
            best_len = len(needle)
            best = OriginMatch(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                crm_origin=campaign.crm_origin,
                match_text=campaign.match_text,
            )
    return best


def resolve_conversation_origin(
    db: Session,
    conversation_id: int,
    *,
    default_origin: str = DEFAULT_CRM_ORIGIN,
) -> tuple[str, OriginMatch | None]:
    """Origem a enviar ao CRM + campanha casada (se houver)."""
    match = match_origin_from_texts(
        list_active_campaigns(db),
        inbound_bodies_for_match(db, conversation_id),
    )
    if match is None:
        return default_origin, None
    return match.crm_origin, match
