from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Branch, Campaign
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.legacy_adapter import generate_message, rate_status
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter(prefix="/campaigns")


def _auth(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


@router.get("")
def list_campaigns(request: Request, db: Session = Depends(get_db)):
    user, redirect = _auth(request, db)
    if redirect:
        return redirect
    stmt = select(Campaign).options(selectinload(Campaign.branch)).order_by(Campaign.created_at.desc())
    if user.role == "SUPERVISOR":
        stmt = stmt.where(Campaign.branch_id == user.branch_id)
    campaigns = list(db.scalars(stmt))
    branches = list(db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)))
    preview = generate_message(
        "empresa local",
        1,
        "WHATSAPP",
        {"business_name": "Empresa Exemplo", "contact_name": "Ana"},
    )
    return render(
        request,
        "campaigns/list.html",
        user=user,
        active_nav="campaigns",
        page_title="Campanhas",
        campaigns=campaigns,
        branches=branches,
        rate_status=rate_status(),
        message_preview=preview,
    )


@router.post("")
def create_campaign(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    segment: str = Form(""),
    team: str = Form("B2B"),
    channel: str = Form("WHATSAPP"),
    daily_limit: int = Form(30),
    branch_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user, redirect = _auth(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    sequence = [
        {"day": 1, "label": "Abordagem inicial"},
        {"day": 3, "label": "Follow-up de valor"},
        {"day": 10, "label": "Retomada consultiva"},
        {"day": 20, "label": "Última tentativa"},
    ]
    limit = max(1, min(daily_limit, 200 if channel == "EMAIL" else 30))
    campaign = Campaign(
        name=name.strip(),
        segment=segment.strip() or None,
        team=team,
        channel=channel,
        daily_limit=limit,
        branch_id=branch_id if user.role in {"ADMIN", "GESTOR"} else user.branch_id,
        created_by_id=user.id,
        status="RASCUNHO",
        sequence_json=json.dumps(sequence, ensure_ascii=False),
    )
    db.add(campaign)
    db.flush()
    write_audit(
        db,
        user=user,
        action="CAMPAIGN_CREATED",
        entity_type="campaign",
        entity_id=campaign.id,
        payload={"channel": channel, "daily_limit": limit},
    )
    db.commit()
    flash(request, "Campanha criada como rascunho. A aprovação gerencial é obrigatória.")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/{campaign_id}/approve")
def approve_campaign(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _auth(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    campaign = db.get(Campaign, campaign_id)
    if campaign:
        campaign.status = "APROVADA"
        campaign.approved_by_id = user.id
        campaign.approved_at = utcnow()
        write_audit(
            db,
            user=user,
            action="CAMPAIGN_APPROVED",
            entity_type="campaign",
            entity_id=campaign.id,
        )
        db.commit()
        flash(request, "Campanha aprovada. O disparo continua desativado até a integração ser validada.")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/{campaign_id}/pause")
def pause_campaign(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _auth(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(Campaign, campaign_id)
    if campaign:
        campaign.status = "PAUSADA"
        write_audit(db, user=user, action="CAMPAIGN_PAUSED", entity_type="campaign", entity_id=campaign.id)
        db.commit()
        flash(request, "Campanha pausada.", "warning")
    return RedirectResponse("/campaigns", status_code=303)
