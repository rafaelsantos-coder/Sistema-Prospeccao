from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Branch, Lead, ProspectingJob, User
from app.security import get_current_user, validate_csrf
from app.services.apify import ApifyNotConfigured, ApifySearchError, ApifyService
from app.services.audit import write_audit
from app.services.followups import initial_next_action
from app.services.queue_routing import (
    available_queues_for_user,
    bind_prospecting_queue,
    get_queue,
    queue_for_prospecting_job,
    set_lead_queue_preference,
)
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter(prefix="/prospecting")


def _run_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(ProspectingJob, job_id)
        if not job:
            return
        job.status = "EXECUTANDO"
        job.started_at = utcnow()
        db.commit()
        try:
            service_queue = queue_for_prospecting_job(db, job.id)
            results = asyncio.run(
                ApifyService().search(segment=job.segment, city=job.city, limit=job.limit)
            )
            job.found_count = len(results)
            imported = 0
            for item in results:
                conditions = []
                if item.get("phone"):
                    conditions.append(Lead.phone == item["phone"])
                if item.get("email"):
                    conditions.append(Lead.email == item["email"])
                if conditions and db.scalar(select(Lead.id).where(or_(*conditions))):
                    continue
                lead = Lead(
                    business_name=item["business_name"],
                    phone=item.get("phone"),
                    email=item.get("email"),
                    website=item.get("website"),
                    segment=item["segment"],
                    city=item["city"],
                    address=item.get("address"),
                    source="APIFY",
                    origin_channel="WHATSAPP",
                    team=job.team,
                    stage="NOVO",
                    score=item["score"],
                    legacy_score=item.get("legacy_score"),
                    temperature=item["temperature"],
                    potential=item.get("potential", ""),
                    raw_data=json.dumps(item.get("raw_data", {}), ensure_ascii=False, default=str),
                    branch_id=job.branch_id,
                    assigned_user_id=job.created_by_id,
                    created_by_id=job.created_by_id,
                    next_action_at=initial_next_action(job.team),
                )
                db.add(lead)
                db.flush()
                if service_queue:
                    set_lead_queue_preference(
                        db,
                        lead.id,
                        service_queue.id,
                        source="PROSPECTING",
                    )
                imported += 1
            job.imported_count = imported
            job.status = "CONCLUIDO"
        except Exception as exc:
            job.status = "FALHOU"
            job.error = str(exc)[:1000]
        finally:
            job.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


@router.get("")
def prospecting_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    stmt = (
        select(ProspectingJob)
        .options(selectinload(ProspectingJob.branch), selectinload(ProspectingJob.created_by))
        .order_by(ProspectingJob.created_at.desc())
        .limit(20)
    )
    if user.role == "SUPERVISOR":
        stmt = stmt.where(ProspectingJob.branch_id == user.branch_id)
    elif user.role == "CONSULTOR":
        stmt = stmt.where(ProspectingJob.created_by_id == user.id)
    jobs = list(db.scalars(stmt))
    branches = list(db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)))
    service_queues = available_queues_for_user(db, user)
    job_queues = {
        job.id: queue_for_prospecting_job(db, job.id)
        for job in jobs
    }
    return render(
        request,
        "prospecting/index.html",
        user=user,
        active_nav="prospecting",
        page_title="Prospecção automatizada",
        jobs=jobs,
        branches=branches,
        service_queues=service_queues,
        job_queues=job_queues,
        apify_configured=settings.integrations["apify"],
    )


@router.post("")
def create_job(
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
    segment: str = Form(...),
    city: str = Form(...),
    limit: int = Form(20),
    team: str = Form("B2B"),
    branch_id: int | None = Form(None),
    queue_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    selected_queue = get_queue(db, queue_id)
    if queue_id and not selected_queue:
        flash(request, "A fila selecionada não está ativa.", "warning")
        return RedirectResponse("/prospecting", status_code=303)
    if selected_queue and selected_queue.id not in {item.id for item in available_queues_for_user(db, user)}:
        flash(request, "Você não possui acesso a esta fila.", "error")
        return RedirectResponse("/prospecting", status_code=303)
    job = ProspectingJob(
        segment=segment.strip(),
        city=city.strip(),
        limit=min(max(limit, 1), 100),
        team=team,
        branch_id=branch_id if user.role in {"ADMIN", "GESTOR"} else user.branch_id,
        created_by_id=user.id,
        status="PENDENTE",
    )
    db.add(job)
    db.flush()
    if selected_queue:
        bind_prospecting_queue(db, job.id, selected_queue.id)
    write_audit(
        db,
        user=user,
        action="PROSPECTING_JOB_CREATED",
        entity_type="prospecting_job",
        entity_id=job.id,
        payload={
            "segment": segment,
            "city": city,
            "limit": limit,
            "queue_id": selected_queue.id if selected_queue else None,
        },
    )
    db.commit()
    background_tasks.add_task(_run_job, job.id)
    flash(request, "Busca criada. O processamento será executado pelo servidor.")
    return RedirectResponse("/prospecting", status_code=303)
