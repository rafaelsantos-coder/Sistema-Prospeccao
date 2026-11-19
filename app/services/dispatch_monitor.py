"""Leitura operacional dos disparos (sem alterar ritmo ou destinatários)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CampaignDispatch, CampaignDispatchRecipient
from app.services.dispatch_queue import dispatch_counts, window_next_start
from app.timeutils import LOCAL_TZ, local_day_start_utc, utc_naive, utc_to_local, utcnow


def daily_capacity(dispatch: CampaignDispatch) -> int:
    """Capacidade máxima teórica sob intervalo, horário e teto diário atuais."""
    cap = max(1, int(dispatch.daily_limit or 1))
    if not dispatch.window_start or not dispatch.window_end:
        return min(cap, ceil(86400 / max(1, int(dispatch.interval_seconds or 1))))
    opening = datetime.strptime(dispatch.window_start, "%H:%M")
    closing = datetime.strptime(dispatch.window_end, "%H:%M")
    available_seconds = max(0, int((closing - opening).total_seconds()))
    return min(cap, max(1, ceil(available_seconds / max(1, int(dispatch.interval_seconds or 1)))))


def _display_datetime(value: datetime | None) -> str | None:
    localized = utc_to_local(value)
    return localized.strftime("%d/%m/%Y às %H:%M") if localized else None


def _next_local_midnight(now: datetime) -> datetime:
    today_local = utc_to_local(now).date()
    return datetime.combine(
        today_local + timedelta(days=1), time.min, tzinfo=LOCAL_TZ
    ).astimezone(UTC).replace(tzinfo=None)


def dispatch_live_snapshot(db: Session, dispatch: CampaignDispatch) -> dict[str, object]:
    """Resumo para o gestor. Não altera status, contadores nem horário do disparo."""
    now = utcnow()
    counts = dispatch_counts(db, dispatch.id)
    sent_today = int(
        db.scalar(
            select(func.count(CampaignDispatchRecipient.id)).where(
                CampaignDispatchRecipient.dispatch_id == dispatch.id,
                CampaignDispatchRecipient.status == "ENVIADO",
                CampaignDispatchRecipient.sent_at >= local_day_start_utc(),
            )
        )
        or 0
    )
    last_sent = db.scalar(
        select(func.max(CampaignDispatchRecipient.sent_at)).where(
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
            CampaignDispatchRecipient.status == "ENVIADO",
        )
    )
    next_send = utc_naive(dispatch.next_send_at) or now
    limit = max(1, int(dispatch.daily_limit or 1))
    window_wait = window_next_start(dispatch, now)
    if sent_today >= limit:
        next_send = max(next_send, _next_local_midnight(now))
    if window_wait is not None:
        next_send = max(next_send, window_wait)
    # Se o intervalo terminar fora da janela, aguardar o início do dia seguinte.
    scheduled_window = window_next_start(dispatch, next_send)
    if scheduled_window is not None:
        next_send = max(next_send, scheduled_window)

    if dispatch.status == "PAUSADO":
        label = "Pausado manualmente"
    elif dispatch.status == "CANCELADO":
        label = "Cancelado"
    elif dispatch.status == "CONCLUIDO":
        label = "Concluído"
    elif dispatch.status != "ATIVO":
        label = "Aguardando início"
    elif counts["pending"] + counts["sending"] == 0 and counts["unknown"]:
        label = "Envios a conferir: resultado externo incerto"
    elif counts["pending"] + counts["sending"] == 0:
        label = "Sem destinatários pendentes"
    elif sent_today >= limit:
        label = f"Limite diário de {limit} atingido"
    elif window_wait is not None:
        label = "Fora do horário permitido"
    elif next_send > now:
        label = f"Aguardando intervalo de {dispatch.interval_seconds}s"
    else:
        label = "Aguardando próximo ciclo do executor"

    capacity = daily_capacity(dispatch)
    remaining = counts["pending"] + counts["sending"]
    return {
        "status": dispatch.status,
        "status_label": label,
        "last_sent": _display_datetime(last_sent),
        "next_send": (
            _display_datetime(next_send) if dispatch.status == "ATIVO" and remaining else None
        ),
        "sent_today": sent_today,
        "daily_limit": limit,
        "daily_capacity": capacity,
        "estimated_days_remaining": ceil(remaining / capacity),
        "counts": counts,
        "checked_at": _display_datetime(now),
    }
