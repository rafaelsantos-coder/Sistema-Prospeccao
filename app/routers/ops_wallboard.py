"""Painel operacional gerencial — visão ao vivo de filas, conversas e alertas."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.security import get_current_user, require_roles
from app.services.ops_wallboard import build_revision, safe_build_ops_wallboard
from app.web import render

router = APIRouter()
LOG = logging.getLogger("sulnet_ops_wallboard")


def _user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


@router.get("/operations")
def operations_wallboard(
    request: Request,
    queue: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR")
    snapshot = safe_build_ops_wallboard(db, user, queue_id=queue)
    response = render(
        request,
        "operations/wallboard.html",
        user=user,
        active_nav="operations",
        page_title="Painel operacional",
        snapshot=snapshot,
        selected_queue=queue,
    )
    response.headers["X-Ops-Revision"] = str(snapshot.get("revision") or build_revision())
    return response


@router.get("/api/operations/wallboard")
def operations_wallboard_api(
    request: Request,
    queue: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"detail": "Não autenticado"}, status_code=401)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR")
    snapshot = safe_build_ops_wallboard(db, user, queue_id=queue)
    return JSONResponse(
        snapshot,
        headers={"X-Ops-Revision": str(snapshot.get("revision") or build_revision())},
    )


@router.get("/api/operations/revision")
def operations_revision():
    """Endpoint leve para confirmar se o deploy chegou na produção."""
    return {"revision": build_revision()}
