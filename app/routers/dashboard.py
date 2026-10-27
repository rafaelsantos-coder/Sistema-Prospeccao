from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.security import get_current_user
from app.services.legacy_adapter import rate_status
from app.services.metrics import dashboard_metrics, recent_activities, urgent_leads
from app.timeutils import utcnow
from app.web import render


router = APIRouter()


@router.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/dashboard" if get_current_user(request, db) else "/login", status_code=303)


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    )
