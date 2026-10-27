from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Lead
from app.security import get_current_user
from app.services.metrics import apply_user_scope, dashboard_metrics


router = APIRouter(prefix="/api")


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return {"status": "ok", "app": settings.app_name, "environment": settings.app_env}


@router.get("/dashboard")
def dashboard_api(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return {"error": "unauthorized"}
    return dashboard_metrics(db, user)


@router.get("/leads")
def leads_api(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return {"error": "unauthorized"}
    stmt = apply_user_scope(select(Lead).order_by(Lead.created_at.desc()).limit(min(limit, 100)), user)
    leads = list(db.scalars(stmt))
    return {
        "items": [
            {
                "id": item.id,
                "business_name": item.business_name,
                "contact_name": item.contact_name,
                "phone": item.phone,
                "email": item.email,
                "city": item.city,
                "team": item.team,
                "stage": item.stage,
                "score": item.score,
                "temperature": item.temperature,
                "source": item.source,
                "is_paid_traffic": item.is_paid_traffic,
            }
            for item in leads
        ]
    }
