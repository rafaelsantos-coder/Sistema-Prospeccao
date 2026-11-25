"""Campanhas de anúncio: texto padrão da mensagem → origem enviada ao CRM."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Message, OriginCampaign
from app.services.origin_campaign_catalog import PAID_TRAFFIC_ORIGIN_CAMPAIGNS

DEFAULT_CRM_ORIGIN = "Sulnet Prospecta"
MIN_MATCH_CHARS = 12
MAX_INBOUND_SCAN = 12

# Marca d'água do Meta no prefill do WhatsApp (Cf = format controls).
_FINGERPRINT_CATEGORIES = frozenset({"Cf"})


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


def fingerprint_sequence(value: str | None) -> str:
    """Extrai a sequência de caracteres invisíveis usada como fingerprint do anúncio."""
    return "".join(
        ch
        for ch in (value or "")
        if unicodedata.category(ch) in _FINGERPRINT_CATEGORIES
    )


def _normalize_match_text_preserve_fingerprint(value: str | None) -> str:
    """Trim + colapsa espaços visíveis, preservando ZWSP/ZWNJ/WJ/BOM do Meta."""
    text = (value or "").strip()
    return re.sub(r"[^\S\u200b\u200c\u200d\u2060\ufeff]+", " ", text).strip()


def validate_campaign_fields(
    *,
    name: str,
    match_text: str,
    crm_origin: str,
    link: str = "",
) -> tuple[str, str, str, str]:
    clean_name = " ".join((name or "").strip().split())
    clean_match = _normalize_match_text_preserve_fingerprint(match_text)
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
    """Escolhe a campanha cujo texto padrão está contido na mensagem (mais longo vence).

    Se a campanha tiver fingerprint invisível do Meta, exige a mesma sequência
    no corpo bruto da mensagem — assim PERFORMANCE de cidades distintas não
    colidem quando o copy visível é igual.
    """
    if not campaigns or not message_bodies:
        return None
    raw_bodies = [body for body in message_bodies if (body or "").strip()]
    if not raw_bodies:
        return None
    folded_bodies = [fold_match_text(body) for body in raw_bodies]

    best: OriginMatch | None = None
    best_len = -1
    best_fp = -1
    for campaign in campaigns:
        if not campaign.active:
            continue
        needle = fold_match_text(campaign.match_text)
        if len(needle) < MIN_MATCH_CHARS:
            continue
        fingerprint = fingerprint_sequence(campaign.match_text)
        matched = False
        for raw, folded in zip(raw_bodies, folded_bodies, strict=True):
            if needle not in folded:
                continue
            if fingerprint and fingerprint not in raw:
                continue
            matched = True
            break
        if not matched:
            continue
        fp_score = len(fingerprint) if fingerprint else 0
        if len(needle) > best_len or (len(needle) == best_len and fp_score > best_fp):
            best_len = len(needle)
            best_fp = fp_score
            best = OriginMatch(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                crm_origin=campaign.crm_origin,
                match_text=campaign.match_text,
            )
    return best


def upsert_paid_traffic_origin_campaigns(db: Session) -> dict[str, int]:
    """Garante as campanhas de tráfego pago ativas (idempotente por nome)."""
    created = updated = unchanged = 0
    for spec in PAID_TRAFFIC_ORIGIN_CAMPAIGNS:
        name, match_text, crm_origin, link = validate_campaign_fields(
            name=spec["name"],
            match_text=spec["match_text"],
            crm_origin=spec["crm_origin"],
            link=spec.get("link", ""),
        )
        existing = db.scalar(
            select(OriginCampaign).where(OriginCampaign.name == name)
        )
        if existing is None:
            db.add(
                OriginCampaign(
                    name=name,
                    match_text=match_text,
                    crm_origin=crm_origin,
                    link=link,
                    active=True,
                )
            )
            created += 1
            continue
        changed = False
        if existing.match_text != match_text:
            existing.match_text = match_text
            changed = True
        if existing.crm_origin != crm_origin:
            existing.crm_origin = crm_origin
            changed = True
        if (existing.link or "") != link:
            existing.link = link
            changed = True
        if not existing.active:
            existing.active = True
            changed = True
        if changed:
            updated += 1
        else:
            unchanged += 1
    return {"created": created, "updated": updated, "unchanged": unchanged}


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
