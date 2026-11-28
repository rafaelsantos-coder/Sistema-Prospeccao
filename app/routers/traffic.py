"""Gestor de Tráfego Pago: módulo de anúncios (Meta/Google) até a venda."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from app.database import get_db
from app.security import get_current_user, validate_csrf
from app.services.meta_ads import PERIODS, MetaAdsError, check_connection, fetch_campaigns
from app.services.meta_ads_write import AdSpec, CampaignPlan, create_paused_campaign
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


DEFAULT_ADS = [
    AdSpec("A1 - Arte - Preço 650 Mega", "image", "650 Mega por R$ 99,90",
           "650 Mega por R$ 99,90 por mês, com Wi-Fi incluído, roteador e instalação grátis. Chame no WhatsApp e peça a sua em Santo Ângelo.",
           "Instalação e roteador grátis", "a1_arte_preco_650_mega"),
    AdSpec("A2 - Arte - Pague em fevereiro", "image", "Primeira mensalidade só em fevereiro",
           "Contrate agora e pague a primeira mensalidade só em fevereiro de 2027. Roteador e instalação grátis. Chame no WhatsApp.",
           "Fibra óptica Sulnet", "a2_arte_pague_em_fevereiro"),
    AdSpec("V1 - Vídeo - Dor", "video", "Fibra óptica em Santo Ângelo",
           "Sua internet trava na melhor parte do jogo ou daquela chamada importante? Em Santo Ângelo, a fibra óptica da Sulnet resolve. Chame no WhatsApp e veja os planos.",
           "Atendimento de quem é daqui", "v1_video_dor"),
    AdSpec("V2 - Vídeo - Atendimento local", "video", "Sulnet: 20 anos pertinho de você",
           "Internet boa e atendimento de quem está aqui, pertinho de você. São 20 anos cuidando de quem mora em Santo Ângelo. Fale com a gente no WhatsApp.",
           "Chame no WhatsApp", "v2_video_atendimento_local"),
]
DEFAULT_WELCOME = "Olá! Vi o anúncio da Sulnet em Santo Ângelo e quero saber mais sobre os planos."


@router.get("/campaigns/new")
def traffic_campaign_new(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "traffic/campaign_new.html", user=user, active_nav="traffic",
                  ads=DEFAULT_ADS, welcome=DEFAULT_WELCOME, result=None, error="")


@router.post("/campaigns/new")
async def traffic_campaign_create(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    daily_budget: float = Form(...),
    phone: str = Form(...),
    welcome: str = Form(DEFAULT_WELCOME),
    image: UploadFile = File(...),
    video: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Cria a campanha de teste na conta da BM03. Tudo nasce PAUSADO."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    plan = CampaignPlan(
        name=name.strip()[:120], daily_budget_brl=daily_budget, city_name="Santo Ângelo", region_name="Rio Grande do Sul",
        phone="".join(ch for ch in phone if ch.isdigit()), welcome_text=welcome.strip()[:300],
        utm_campaign="sistema_proprio_whatsapp_santo_angelo", ads=list(DEFAULT_ADS),
    )
    result, error = None, ""
    try:
        image_bytes = await image.read()
        result = await run_in_threadpool(
            lambda: create_paused_campaign(
                plan, image=(image.filename or "arte.jpg", image_bytes), video=(video.filename or "video.mp4", video.file),
            )
        )
    except MetaAdsError as exc:
        error = str(exc)
    return render(request, "traffic/campaign_new.html", user=user, active_nav="traffic",
                  ads=DEFAULT_ADS, welcome=welcome, result=result, error=error)
