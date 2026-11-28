"""Gestor de Tráfego Pago: módulo de anúncios (Meta/Google) até a venda."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.security import get_current_user
from app.services.meta_ads import check_connection
from app.web import render

router = APIRouter(prefix="/traffic")


@router.get("")
def traffic_home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "traffic/index.html", user=user, active_nav="traffic")


@router.get("/connection")
def traffic_connection(request: Request, db: Session = Depends(get_db)):
    """Teste de conexão com a Meta. Só faz leituras; nunca cria nem altera anúncios."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "traffic/connection.html", user=user, active_nav="traffic", report=check_connection())
