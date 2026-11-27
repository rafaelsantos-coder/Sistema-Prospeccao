"""Painel de homologacao OpenWA: sem acesso ao banco de contatos ou mensagens."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.security import get_current_user, require_roles, validate_csrf
from app.services.openwa import OpenWAClient, OpenWAConfig, OpenWAError
from app.web import render

router = APIRouter(prefix="/settings/openwa")


def _authorize(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    require_roles(user, "ADMIN", "GESTOR")
    return user, None


def _page(request: Request, user, *, result=None, error=None):
    config = OpenWAConfig.from_env()
    return render(
        request,
        "settings_openwa.html",
        user=user,
        active_nav="settings",
        settings_section="openwa",
        page_title="OpenWA - homologacao isolada",
        openwa_enabled=config.enabled,
        openwa_configured=config.is_configured,
        openwa_host=config.allowed_host,
        openwa_session=config.session_id,
        openwa_result=result,
        openwa_error=error,
    )


@router.get("")
def openwa_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _authorize(request, db)
    if redirect:
        return redirect
    return _page(request, user)


@router.post("/test")
def openwa_test(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _authorize(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    try:
        result = OpenWAClient(OpenWAConfig.from_env()).status()
    except OpenWAError as exc:
        return _page(request, user, error=str(exc))
    return _page(request, user, result=result)
