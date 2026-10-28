from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse, Response

from app.constants import PIPELINE_STAGES
from app.database import get_db
from app.models import Activity, Branch, Lead, User
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.followups import initial_next_action, next_followup_after
from app.services.metrics import apply_user_scope
from app.services.queue_routing import available_queues_for_user, preferred_queue_for_lead
from app.services.scoring import calculate_sulnet_score, normalize_phone
from app.timeutils import local_input_to_utc, utcnow
from app.web import flash, render


router = APIRouter(prefix="/leads")
PAGE_SIZE = 25


def _current_user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _scoped_lead(db: Session, user: User, lead_id: int) -> Lead | None:
    stmt = (
        select(Lead)
        .options(
            selectinload(Lead.activities).selectinload(Activity.user),
            selectinload(Lead.branch),
            selectinload(Lead.assigned_user),
        )
        .where(Lead.id == lead_id)
    )
    stmt = apply_user_scope(stmt, user)
    return db.scalar(stmt)


def _parse_money(value: str | None) -> Decimal:
    if not value:
        return Decimal("0")
    normalized = value.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal("0")


def _parse_datetime_local(value: str | None) -> datetime | None:
    return local_input_to_utc(value)


@router.get("")
def list_leads(
    request: Request,
    q: str = Query(""),
    stage: str = Query(""),
    team: str = Query(""),
    source: str = Query(""),
    branch: int | None = Query(None),
    paid: str = Query(""),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect

    filters = []
    if q:
        term = f"%{q.strip()}%"
        filters.append(
            or_(
                Lead.business_name.ilike(term),
                Lead.contact_name.ilike(term),
                Lead.phone.ilike(term),
                Lead.email.ilike(term),
                Lead.cnpj.ilike(term),
            )
        )
    if stage:
        filters.append(Lead.stage == stage)
    if team:
        filters.append(Lead.team == team)
    if source:
        filters.append(Lead.source == source)
    if branch:
        filters.append(Lead.branch_id == branch)
    if paid == "sim":
        filters.append(Lead.is_paid_traffic.is_(True))
    elif paid == "nao":
        filters.append(Lead.is_paid_traffic.is_(False))

    count_stmt = apply_user_scope(select(func.count(Lead.id)).where(*filters), user)
    total = db.scalar(count_stmt) or 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)

    stmt = (
        select(Lead)
        .options(selectinload(Lead.branch), selectinload(Lead.assigned_user))
        .where(*filters)
        .order_by(Lead.priority.desc(), Lead.next_action_at.asc().nullslast(), Lead.score.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    stmt = apply_user_scope(stmt, user)
    leads = list(db.scalars(stmt))

    branches = list(db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)))
    sellers_stmt = select(User).where(
        User.active.is_(True),
        User.role == "CONSULTOR",
    ).order_by(User.name)
    if user.role == "SUPERVISOR" and user.branch_id:
        sellers_stmt = sellers_stmt.where(User.branch_id == user.branch_id)
    sellers = list(db.scalars(sellers_stmt))
    current_list_url = "/leads?" + urlencode(
        {
            "page": page,
            "q": q,
            "stage": stage,
            "team": team,
            "source": source,
            "paid": paid,
            **({"branch": branch} if branch else {}),
        }
    )
    return render(
        request,
        "leads/list.html",
        user=user,
        active_nav="leads",
        page_title="Leads e oportunidades",
        leads=leads,
        branches=branches,
        sellers=sellers,
        current_list_url=current_list_url,
        filters={"q": q, "stage": stage, "team": team, "source": source, "branch": branch, "paid": paid},
        pagination={"page": page, "pages": pages, "total": total},
    )


@router.get("/new")
def new_lead_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    branches = list(db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)))
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)))
    return render(
        request,
        "leads/form.html",
        user=user,
        active_nav="leads",
        page_title="Novo lead",
        lead=None,
        branches=branches,
        users=users,
    )


@router.post("/new")
def create_lead(
    request: Request,
    csrf_token: str = Form(...),
    business_name: str = Form(...),
    contact_name: str = Form(""),
    cnpj: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    segment: str = Form("Não informado"),
    city: str = Form("Santa Rosa"),
    neighborhood: str = Form(""),
    address: str = Form(""),
    source: str = Form("MANUAL"),
    origin_channel: str = Form("WHATSAPP"),
    team: str = Form("LOJA"),
    stage: str = Form("NOVO"),
    priority: str = Form("NORMAL"),
    branch_id: int | None = Form(None),
    assigned_user_id: int | None = Form(None),
    estimated_mrr: str = Form("0"),
    notes: str = Form(""),
    do_not_contact: bool = Form(False),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    normalized_phone = normalize_phone(phone)
    duplicate = None
    if normalized_phone or email.strip():
        conditions = []
        if normalized_phone:
            conditions.append(Lead.phone == normalized_phone)
        if email.strip():
            conditions.append(Lead.email == email.strip().lower())
        duplicate = db.scalar(select(Lead).where(or_(*conditions)))
    if duplicate:
        flash(request, f"Já existe um lead semelhante: {duplicate.business_name}.", "warning")
        return RedirectResponse(f"/leads/{duplicate.id}", status_code=303)

    score_data = {
        "phone": normalized_phone,
        "email": email,
        "website": website,
        "segment": segment,
        "city": city,
        "source": source,
        "do_not_contact": do_not_contact,
    }
    score = calculate_sulnet_score(score_data)
    assignee = assigned_user_id if user.role in {"ADMIN", "GESTOR", "SUPERVISOR"} else user.id
    branch_value = branch_id if user.role in {"ADMIN", "GESTOR"} else user.branch_id
    is_paid = source == "TRAFEGO_PAGO"

    lead = Lead(
        business_name=business_name.strip(),
        contact_name=contact_name.strip(),
        cnpj=cnpj.strip() or None,
        phone=normalized_phone,
        email=email.strip().lower() or None,
        website=website.strip() or None,
        segment=segment.strip() or "Não informado",
        city=city.strip() or "Santa Rosa",
        neighborhood=neighborhood.strip() or None,
        address=address.strip() or None,
        source=source,
        origin_channel=origin_channel,
        team=team,
        stage="NAO_CONTATAR" if do_not_contact else stage,
        score=score.score,
        temperature=score.temperature,
        priority=priority,
        potential=", ".join(score.reasons),
        estimated_mrr=_parse_money(estimated_mrr),
        notes=notes.strip(),
        is_paid_traffic=is_paid,
        do_not_contact=do_not_contact,
        next_action_at=None if do_not_contact else initial_next_action(team),
        branch_id=branch_value,
        assigned_user_id=assignee,
        created_by_id=user.id,
    )
    db.add(lead)
    db.flush()
    write_audit(
        db,
        user=user,
        action="LEAD_CREATED",
        entity_type="lead",
        entity_id=lead.id,
        payload={"source": source, "team": team, "paid_traffic": is_paid},
    )
    db.commit()
    flash(request, "Lead cadastrado e primeira ação programada.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.get("/export.csv")
def export_csv(request: Request, db: Session = Depends(get_db)):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    stmt = apply_user_scope(select(Lead).order_by(Lead.created_at.desc()), user)
    leads = list(db.scalars(stmt))
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["ID", "Empresa", "Contato", "Telefone", "E-mail", "Cidade", "Equipe", "Etapa", "Score", "Origem", "MRR"])
    for lead in leads:
        writer.writerow([
            lead.id, lead.business_name, lead.contact_name, lead.phone, lead.email,
            lead.city, lead.team, lead.stage, lead.score, lead.source, lead.estimated_mrr,
        ])
    payload = "\ufeff" + stream.getvalue()
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads-sulnet.csv"},
    )


@router.get("/{lead_id}")
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db)):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    lead = _scoped_lead(db, user, lead_id)
    if not lead:
        return render(request, "404.html", user=user, page_title="Lead não encontrado", status_code=404)
    branches = list(db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)))
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)))
    service_queues = available_queues_for_user(db, user)
    preferred_queue = preferred_queue_for_lead(db, lead.id)
    comparison_now = utcnow()
    if lead.next_action_at and lead.next_action_at.tzinfo is not None:
        comparison_now = comparison_now.replace(tzinfo=timezone.utc)
    return render(
        request,
        "leads/detail.html",
        user=user,
        active_nav="leads",
        page_title=lead.business_name,
        lead=lead,
        branches=branches,
        users=users,
        service_queues=service_queues,
        preferred_queue=preferred_queue,
        pipeline=PIPELINE_STAGES,
        current_stage_index=(PIPELINE_STAGES.index(lead.stage) if lead.stage in PIPELINE_STAGES else -1),
        now=comparison_now,
    )


@router.post("/{lead_id}/stage")
def update_stage(
    lead_id: int,
    request: Request,
    stage: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    lead = _scoped_lead(db, user, lead_id)
    if not lead:
        return RedirectResponse("/leads", status_code=303)
    old_stage = lead.stage
    lead.stage = stage
    if stage in {"GANHO", "PERDIDO", "NAO_CONTATAR"}:
        lead.next_action_at = None
    if stage == "NAO_CONTATAR":
        lead.do_not_contact = True
    write_audit(
        db,
        user=user,
        action="LEAD_STAGE_UPDATED",
        entity_type="lead",
        entity_id=lead.id,
        payload={"from": old_stage, "to": stage},
    )
    db.commit()
    flash(request, "Etapa do funil atualizada.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/activity")
def add_activity(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    kind: str = Form(...),
    status: str = Form("CONCLUIDA"),
    channel: str = Form(""),
    outcome: str = Form(""),
    description: str = Form(""),
    scheduled_for: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    lead = _scoped_lead(db, user, lead_id)
    if not lead:
        return RedirectResponse("/leads", status_code=303)
    now = utcnow()
    activity = Activity(
        lead_id=lead.id,
        user_id=user.id,
        kind=kind,
        status=status,
        channel=channel or None,
        outcome=outcome.strip() or None,
        description=description.strip(),
        scheduled_for=_parse_datetime_local(scheduled_for),
        completed_at=now if status == "CONCLUIDA" else None,
    )
    db.add(activity)
    if status == "CONCLUIDA":
        lead.last_contact_at = now
        completed_followups = sum(1 for item in lead.activities if item.kind == "FOLLOW_UP" and item.status == "CONCLUIDA")
        lead.next_action_at = next_followup_after(now, completed_followups)
        if lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
            lead.stage = "CONTATO_REALIZADO"
    elif activity.scheduled_for:
        lead.next_action_at = activity.scheduled_for
    write_audit(
        db,
        user=user,
        action="ACTIVITY_CREATED",
        entity_type="lead",
        entity_id=lead.id,
        payload={"kind": kind, "status": status},
    )
    db.commit()
    flash(request, "Atividade registrada.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/assign")
def assign_lead(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    assigned_user_id: int = Form(...),
    branch_id: int | None = Form(None),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _current_user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR")
    lead = _scoped_lead(db, user, lead_id)
    if not lead:
        return RedirectResponse("/leads", status_code=303)

    target = db.scalar(
        select(User).where(
            User.id == assigned_user_id,
            User.active.is_(True),
            User.role == "CONSULTOR",
        )
    )
    if not target:
        flash(request, "Vendedor inválido ou inativo.", "warning")
        return RedirectResponse("/leads", status_code=303)
    if user.role == "SUPERVISOR" and user.branch_id and target.branch_id != user.branch_id:
        flash(request, "Você só pode transferir leads para vendedores da sua unidade.", "warning")
        return RedirectResponse("/leads", status_code=303)

    previous_user_id = lead.assigned_user_id
    lead.assigned_user_id = target.id
    if branch_id and user.role in {"ADMIN", "GESTOR"}:
        lead.branch_id = branch_id

    write_audit(
        db,
        user=user,
        action="LEAD_TRANSFERRED",
        entity_type="lead",
        entity_id=lead.id,
        payload={
            "from_user_id": previous_user_id,
            "to_user_id": target.id,
            "to_user_name": target.name,
            "branch_id": lead.branch_id,
        },
    )
    db.commit()
    flash(request, f"Lead transferida para {target.name}.")

    destination = return_to.strip()
    if not destination.startswith("/leads"):
        destination = f"/leads/{lead.id}"
    return RedirectResponse(destination, status_code=303)
