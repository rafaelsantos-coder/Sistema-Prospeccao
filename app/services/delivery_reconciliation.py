"""Consulta gerencial somente leitura de envios com resultado incerto.

Não reenvia, não altera estados, não chama Z-API/Telegram nem executa IA.
Os envios recentes em ENVIANDO não são tratados como falha: aparecem somente
após o prazo mínimo de 15 minutos sem atualização.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CampaignDispatch,
    CampaignDispatchRecipient,
    CampaignRecipient,
)
from app.timeutils import utcnow


REVIEW_STALE_MINUTES = 15
REVIEW_PAGE_SIZE = 50
REVIEW_MAX_PAGE = 200
REVIEW_STATUSES = frozenset({"TODOS", "INDETERMINADO", "ENVIANDO"})


def _review_filter(model, status: str, cutoff):
    uncertain = model.status == "INDETERMINADO"
    stalled = and_(model.status == "ENVIANDO", model.updated_at <= cutoff)
    if status == "INDETERMINADO":
        return uncertain
    if status == "ENVIANDO":
        return stalled
    return or_(uncertain, stalled)


def reconciliation_snapshot(db: Session, *, status: str = "TODOS", page: int = 1):
    """Resultados limitados por página, inclusive total por categoria."""
    if status not in REVIEW_STATUSES:
        raise ValueError("Filtro de status inválido.")
    if not 1 <= page <= REVIEW_MAX_PAGE:
        raise ValueError("Número da página inválido.")

    cutoff = utcnow() - timedelta(minutes=REVIEW_STALE_MINUTES)
    offset = (page - 1) * REVIEW_PAGE_SIZE
    dispatched_filter = _review_filter(CampaignDispatchRecipient, status, cutoff)
    legacy_filter = _review_filter(CampaignRecipient, status, cutoff)

    dispatched_total = int(
        db.scalar(
            select(func.count(CampaignDispatchRecipient.id)).where(dispatched_filter)
        ) or 0
    )
    legacy_total = int(
        db.scalar(select(func.count(CampaignRecipient.id)).where(legacy_filter)) or 0
    )

    dispatched = list(
        db.scalars(
            select(CampaignDispatchRecipient)
            .options(
                selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.queue),
                selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.campaign),
            )
            .where(dispatched_filter)
            .order_by(
                CampaignDispatchRecipient.updated_at.desc(),
                CampaignDispatchRecipient.id.desc(),
            )
            .offset(offset)
            .limit(REVIEW_PAGE_SIZE)
        )
    )
    legacy = list(
        db.scalars(
            select(CampaignRecipient)
            .options(
                selectinload(CampaignRecipient.campaign),
                selectinload(CampaignRecipient.lead),
            )
            .where(legacy_filter)
            .order_by(CampaignRecipient.updated_at.desc(), CampaignRecipient.id.desc())
            .offset(offset)
            .limit(REVIEW_PAGE_SIZE)
        )
    )
    return {
        "status": status,
        "page": page,
        "page_size": REVIEW_PAGE_SIZE,
        "stale_minutes": REVIEW_STALE_MINUTES,
        "cutoff": cutoff,
        "dispatched": dispatched,
        "legacy": legacy,
        "dispatched_total": dispatched_total,
        "legacy_total": legacy_total,
        "total": dispatched_total + legacy_total,
        "has_previous": page > 1,
        "has_next": page * REVIEW_PAGE_SIZE < max(dispatched_total, legacy_total),
    }
