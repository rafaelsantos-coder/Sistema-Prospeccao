"""Consulta gerencial somente leitura de envios com resultado incerto.

Não reenvia, não altera estados, não chama Z-API/Telegram nem executa IA.
Os envios recentes em ENVIANDO não são tratados como falha: aparecem somente
após o prazo mínimo de 15 minutos sem atualização.
"""
from __future__ import annotations

from datetime import timedelta
import csv
import io

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CampaignDispatch,
    CampaignDispatchRecipient,
    CampaignQueueLink,
    CampaignRecipient,
    ServiceQueue,
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


def reconciliation_snapshot(
    db: Session, *, status: str = "TODOS", page: int = 1,
    queue_id: int | None = None,
):
    """Resultados limitados por página, inclusive total por categoria."""
    if status not in REVIEW_STATUSES:
        raise ValueError("Filtro de status inválido.")
    if not 1 <= page <= REVIEW_MAX_PAGE:
        raise ValueError("Número da página inválido.")
    if queue_id is not None and queue_id <= 0:
        raise ValueError("Fila inválida.")

    cutoff = utcnow() - timedelta(minutes=REVIEW_STALE_MINUTES)
    offset = (page - 1) * REVIEW_PAGE_SIZE
    dispatched_filter = _review_filter(CampaignDispatchRecipient, status, cutoff)
    legacy_filter = _review_filter(CampaignRecipient, status, cutoff)

    dispatched_base = select(CampaignDispatchRecipient).where(dispatched_filter)
    legacy_base = select(CampaignRecipient).where(legacy_filter)
    dispatched_count = select(func.count(CampaignDispatchRecipient.id)).where(dispatched_filter)
    legacy_count = select(func.count(CampaignRecipient.id)).where(legacy_filter)
    if queue_id is not None:
        # O vínculo histórico é definido pela campanha no funil legado.
        dispatched_base = dispatched_base.join(
            CampaignDispatch, CampaignDispatchRecipient.dispatch_id == CampaignDispatch.id
        ).where(CampaignDispatch.queue_id == queue_id)
        dispatched_count = dispatched_count.join(
            CampaignDispatch, CampaignDispatchRecipient.dispatch_id == CampaignDispatch.id
        ).where(CampaignDispatch.queue_id == queue_id)
        legacy_base = legacy_base.join(
            CampaignQueueLink, CampaignRecipient.campaign_id == CampaignQueueLink.campaign_id
        ).where(CampaignQueueLink.queue_id == queue_id)
        legacy_count = legacy_count.join(
            CampaignQueueLink, CampaignRecipient.campaign_id == CampaignQueueLink.campaign_id
        ).where(CampaignQueueLink.queue_id == queue_id)

    dispatched_total = int(db.scalar(dispatched_count) or 0)
    legacy_total = int(db.scalar(legacy_count) or 0)

    dispatched = list(
        db.scalars(
            dispatched_base
            .options(
                selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.queue),
                selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.campaign),
            )
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
            legacy_base
            .options(
                selectinload(CampaignRecipient.campaign),
                selectinload(CampaignRecipient.lead),
            )
            .order_by(CampaignRecipient.updated_at.desc(), CampaignRecipient.id.desc())
            .offset(offset)
            .limit(REVIEW_PAGE_SIZE)
        )
    )
    campaign_ids = {item.campaign_id for item in legacy}
    queue_names = {}
    if campaign_ids:
        links = db.scalars(
            select(CampaignQueueLink)
            .options(selectinload(CampaignQueueLink.queue))
            .where(CampaignQueueLink.campaign_id.in_(campaign_ids))
        )
        queue_names = {link.campaign_id: link.queue.name for link in links}

    queues = list(db.execute(select(ServiceQueue.id, ServiceQueue.name).order_by(ServiceQueue.name.asc(), ServiceQueue.id.asc())))

    return {
        "queue_id": queue_id,
        "queues": queues,
        "legacy_queue_names": queue_names,
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


REVIEW_EXPORT_LIMIT = 500


def _safe_csv_cell(value: object) -> str:
    """Evita injeção de fórmulas quando o CSV é aberto no Excel/Sheets."""
    cell = "" if value is None else str(value)
    if cell.lstrip().startswith(("=", "+", "-", "@")) or cell.startswith(("\t", "\r", "\n")):
        return "'" + cell
    return cell


def reconciliation_csv(
    db: Session, *, status: str = "TODOS", queue_id: int | None = None,
) -> str:
    """Exportação limitada, gerencial e somente leitura, das duas origens."""
    if status not in REVIEW_STATUSES:
        raise ValueError("Filtro de status inválido.")
    if queue_id is not None and queue_id <= 0:
        raise ValueError("Fila inválida.")

    cutoff = utcnow() - timedelta(minutes=REVIEW_STALE_MINUTES)
    csv_output = io.StringIO(newline="")
    writer = csv.writer(csv_output, delimiter=";")
    writer.writerow([
        "Origem", "Campanha", "Fila", "Contato", "Telefone", "Situação",
        "Tentativas", "Última atualização (UTC)", "ID do provedor", "Erro",
    ])

    dispatched_stmt = (
        select(CampaignDispatchRecipient)
        .options(
            selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.queue),
            selectinload(CampaignDispatchRecipient.dispatch).selectinload(CampaignDispatch.campaign),
        )
        .where(_review_filter(CampaignDispatchRecipient, status, cutoff))
        .order_by(CampaignDispatchRecipient.updated_at.desc(), CampaignDispatchRecipient.id.desc())
        .limit(REVIEW_EXPORT_LIMIT)
    )
    legacy_stmt = (
        select(CampaignRecipient)
        .options(
            selectinload(CampaignRecipient.campaign),
            selectinload(CampaignRecipient.lead),
        )
        .where(_review_filter(CampaignRecipient, status, cutoff))
        .order_by(CampaignRecipient.updated_at.desc(), CampaignRecipient.id.desc())
        .limit(REVIEW_EXPORT_LIMIT)
    )
    if queue_id is not None:
        dispatched_stmt = dispatched_stmt.join(
            CampaignDispatch, CampaignDispatchRecipient.dispatch_id == CampaignDispatch.id
        ).where(CampaignDispatch.queue_id == queue_id)
        legacy_stmt = legacy_stmt.join(
            CampaignQueueLink, CampaignRecipient.campaign_id == CampaignQueueLink.campaign_id
        ).where(CampaignQueueLink.queue_id == queue_id)

    dispatched = list(db.scalars(dispatched_stmt))
    legacy = list(db.scalars(legacy_stmt))
    legacy_queue_names = {}
    if legacy:
        links = db.scalars(
            select(CampaignQueueLink)
            .options(selectinload(CampaignQueueLink.queue))
            .where(CampaignQueueLink.campaign_id.in_({row.campaign_id for row in legacy}))
        )
        legacy_queue_names = {link.campaign_id: link.queue.name for link in links}

    for row in dispatched:
        writer.writerow([_safe_csv_cell(item) for item in (
            "CSV", row.dispatch.campaign.name, row.dispatch.queue.name, row.name,
            row.phone, row.status, row.attempts,
            row.updated_at.isoformat() if row.updated_at else "",
            row.provider_message_id, row.error,
        )])
    for row in legacy:
        writer.writerow([_safe_csv_cell(item) for item in (
            "FUNIL", row.campaign.name,
            legacy_queue_names.get(row.campaign_id, "Fila não definida"),
            row.lead.business_name or row.lead.contact_name, row.lead.phone,
            row.status, row.attempts,
            row.updated_at.isoformat() if row.updated_at else "",
            row.provider_message_id, row.error,
        )])
    # UTF-8 BOM para importação no Excel brasileiro, com campos separados por ponto-e-vírgula.
    return "\ufeff" + csv_output.getvalue()
