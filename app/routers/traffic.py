"""Gestor de Tráfego Pago: módulo de anúncios (Meta/Google) até a venda."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.security import get_current_user
from app.services.meta_ads import PERIODS, MetaAdsError, check_connection, fetch_campaigns
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


@router.get("/campaigns")
def traffic_campaigns(request: Request, periodo: str = "7d", db: Session = Depends(get_db)):
    """Campanhas da Meta com gasto e conversas no WhatsApp. Só leitura."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if periodo not in PERIODS:
        periodo = "7d"
    rows, error = [], ""
    try:
        rows = fetch_campaigns(periodo)
    except MetaAdsError as exc:
        error = str(exc)
    totals = {
        "spend": sum(r.spend for r in rows),
        "conversations": sum(r.conversations for r in rows),
    }
    totals["cost"] = (totals["spend"] / totals["conversations"]) if totals["conversations"] else None
    return render(
        request, "traffic/campaigns.html", user=user, active_nav="traffic",
        rows=rows, error=error, totals=totals, periodo=periodo,
        periods=[(k, v[1]) for k, v in PERIODS.items()],
    )
