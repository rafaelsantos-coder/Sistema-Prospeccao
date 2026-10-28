from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Branch, Campaign, CampaignQueueLink
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.campaign_queue import campaign_queue_counts, enqueue_campaign
from app.services.legacy_adapter import generate_message, rate_status
from app.services.queue_routing import available_queues_for_user, bind_campaign_queue, get_queue, queue_for_campaign
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
    queue_counts = {campaign.id: campaign_queue_counts(db, campaign.id) for campaign in campaigns}
    service_queues = available_queues_for_user(db, user)
    campaign_queues = {
        campaign.id: queue_for_campaign(db, campaign.id)
        for campaign in campaigns
    }
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
        queue_counts=queue_counts,
        service_queues=service_queues,
        campaign_queues=campaign_queues,
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
    message: str = Form(...),
    queue_id: int | None = Form(None),
    branch_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user, redirect = _auth(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    clean_message = message.strip()
    selected_queue = get_queue(db, queue_id)
    if channel == "WHATSAPP" and not selected_queue:
        flash(request, "Selecione a fila que receberá as respostas desta campanha.", "warning")
        return RedirectResponse("/campaigns", status_code=303)
    if selected_queue and selected_queue.id not in {item.id for item in available_queues_for_user(db, user)}:
        flash(request, "Você não possui acesso a esta fila.", "error")
        return RedirectResponse("/campaigns", status_code=303)
    if not clean_message:
        flash(request, "Informe a mensagem inicial da campanha.", "warning")
        return RedirectResponse("/campaigns", status_code=303)
    sequence = [
        {"day": 1, "label": "Abordagem inicial", "message": clean_message},
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
    if selected_queue:
        bind_campaign_queue(db, campaign.id, selected_queue.id)
    write_audit(
        db,
        user=user,
        action="CAMPAIGN_CREATED",
        entity_type="campaign",
        entity_id=campaign.id,
        payload={
            "channel": channel,
            "daily_limit": limit,
            "queue_id": selected_queue.id if selected_queue else None,
        },
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
        flash(request, "Campanha aprovada. Agora prepare a audiência para colocar os leads na fila de envio.")
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


@router.post("/{campaign_id}/enqueue")
def enqueue_campaign_recipients(
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
    if not campaign:
        return RedirectResponse("/campaigns", status_code=303)
    if campaign.channel != "WHATSAPP":
        flash(request, "A fila automatizada esta liberada inicialmente apenas para WhatsApp.", "warning")
        return RedirectResponse("/campaigns", status_code=303)
    if not queue_for_campaign(db, campaign.id):
        flash(request, "Defina uma fila de atendimento para esta campanha antes do disparo.", "warning")
        return RedirectResponse("/campaigns", status_code=303)
    if campaign.status not in {"APROVADA", "PAUSADA", "ATIVA"}:
        flash(request, "A campanha precisa ser aprovada antes de entrar na fila.", "warning")
        return RedirectResponse("/campaigns", status_code=303)
    if user.role == "SUPERVISOR" and campaign.branch_id != user.branch_id:
        return RedirectResponse("/campaigns", status_code=303)
    if user.role == "CONSULTOR" and campaign.created_by_id != user.id:
        return RedirectResponse("/campaigns", status_code=303)
    try:
        created = enqueue_campaign(db, campaign)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    write_audit(
        db,
        user=user,
        action="CAMPAIGN_ENQUEUED",
        entity_type="campaign",
        entity_id=campaign.id,
        payload={"recipients_added": created},
    )
    db.commit()
    if created:
        flash(request, f"{created} leads adicionados a fila. O worker fara os envios respeitando o limite diario.")
    else:
        flash(request, "Nenhum novo lead elegivel foi encontrado para esta campanha.", "warning")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/{campaign_id}/resume")
def resume_campaign(
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
    if campaign and campaign.status == "PAUSADA":
        campaign.status = "ATIVA"
        write_audit(db, user=user, action="CAMPAIGN_RESUMED", entity_type="campaign", entity_id=campaign.id)
        db.commit()
        flash(request, "Campanha retomada.")
    return RedirectResponse("/campaigns", status_code=303)
