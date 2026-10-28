from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import Branch, Target, User
from app.security import get_current_user
from app.services.legacy_adapter import legacy_modules_available, rate_status
from app.services.zapi import ZApiError, ZApiService
from app.web import render


router = APIRouter(prefix="/settings")


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return RedirectResponse("/dashboard", status_code=303)
    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    users = list(db.scalars(select(User).order_by(User.name)))
    targets = list(db.scalars(select(Target).order_by(Target.team)))
    zapi_status = {
        "configured": settings.integrations["zapi"],
        "connected": False,
        "smartphone_connected": False,
        "error": None,
        "webhook_ready": bool(settings.zapi_webhook_url),
    }
    if settings.integrations["zapi"]:
        try:
            zapi_status.update(ZApiService().status())
        except ZApiError as exc:
            zapi_status["error"] = str(exc)
    return render(
        request,
        "settings.html",
        user=user,
        active_nav="settings",
        page_title="Configurações",
        integrations=settings.integrations,
        zapi_status=zapi_status,
        legacy_available=legacy_modules_available(),
        rate_status=rate_status(),
        branches=branches,
        users=users,
        targets=targets,
        environment=settings.app_env,
    )
