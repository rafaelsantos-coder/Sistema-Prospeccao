from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.security import get_current_user
from app.services.legacy_adapter import rate_status
from app.services.metrics import dashboard_metrics, recent_activities, urgent_leads
from datetime import datetime

from app.timeutils import LOCAL_TZ, utcnow
from app.web import render


router = APIRouter()

_WEEKDAYS_PT = (
    "Segunda",
    "Terça",
    "Quarta",
    "Quinta",
    "Sexta",
    "Sábado",
    "Domingo",
)
_MONTHS_PT = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def _local_date_label(value: datetime) -> str:
    return f"{_WEEKDAYS_PT[value.weekday()]}, {value.day} de {_MONTHS_PT[value.month - 1]}"


@router.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/dashboard" if get_current_user(request, db) else "/login", status_code=303)


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    local_now = datetime.now(LOCAL_TZ)
    return render(
        request,
        "dashboard.html",
        user=user,
        active_nav="dashboard",
        page_title="Visão geral",
        metrics=dashboard_metrics(db, user),
        urgent_leads=urgent_leads(db, user),
        recent_activities=recent_activities(db, user),
        rate_status=rate_status(),
        integrations=settings.integrations,
        now=utcnow(),
        local_now=local_now,
        local_date_label=_local_date_label(local_now),
    )
