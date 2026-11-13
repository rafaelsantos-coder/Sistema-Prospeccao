from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Branch, Campaign, Lead, ProspectingJob, User
from app.security import get_current_user, validate_csrf
from app.services.apify import ApifyService
from app.services.audit import write_audit
from app.services.campaign_queue import campaign_message_template
from app.services.dispatch_queue import create_dispatch_from_leads
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


LOG = logging.getLogger("sulnet_prospecting")

router = APIRouter(prefix="/prospecting")

SEARCH_ONLY = "SEARCH_ONLY"
SEARCH_AND_DISPATCH = "SEARCH_AND_DISPATCH"


def _execution_plan(job: ProspectingJob) -> dict[str, Any]:
    try:
        raw = json.loads(job.execution_json or "{}")
    except (TypeError, ValueError):
        return {"mode": SEARCH_ONLY}
    if not isinstance(raw, dict):
        return {"mode": SEARCH_ONLY}
    mode = str(raw.get("mode") or SEARCH_ONLY).upper()
    if mode not in {SEARCH_ONLY, SEARCH_AND_DISPATCH}:
        mode = SEARCH_ONLY
    return {**raw, "mode": mode}


def _set_execution_plan(job: ProspectingJob, plan: dict[str, Any]) -> None:
    job.execution_json = json.dumps(plan, ensure_ascii=False, default=str)


def _whatsapp_campaigns(db: Session) -> list[Campaign]:
    return list(
        db.scalars(
            select(Campaign)
            .where(
                Campaign.channel == "WHATSAPP",
                Campaign.status.in_({"APROVADA", "ATIVA", "PAUSADA"}),
            )
            .order_by(Campaign.updated_at.desc())
        )
    )


def _can_schedule_dispatch(user: User) -> bool:
    return user.role in {"ADMIN", "GESTOR"}


def _run_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(ProspectingJob, job_id)
        if not job:
            return
        job.status = "EXECUTANDO"
        job.started_at = utcnow()
        db.commit()
        LOG.info("prospecting_job_started job=%s limit=%s", job.id, job.limit)
        try:
            service_queue = queue_for_prospecting_job(db, job.id)
            results = asyncio.run(
                ApifyService().search(segment=job.segment, city=job.city, limit=job.limit)
            )
            job.found_count = len(results)
            imported = 0
            imported_leads: list[Lead] = []
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
                    assigned_user_id=None if service_queue else job.created_by_id,
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
                imported_leads.append(lead)
            job.imported_count = imported

            plan = _execution_plan(job)
            if plan.get("mode") == SEARCH_AND_DISPATCH and imported_leads:
                campaign = db.get(Campaign, int(plan.get("campaign_id") or 0))
                queue = get_queue(db, int(plan.get("queue_id") or 0))
                if (
                    campaign
                    and campaign.channel == "WHATSAPP"
                    and campaign.status in {"APROVADA", "ATIVA", "PAUSADA"}
                    and campaign_message_template(campaign)
                    and queue
                ):
                    dispatch, summary = create_dispatch_from_leads(
                        db,
                        campaign=campaign,
                        queue=queue,
                        leads=imported_leads,
                        created_by_id=job.created_by_id,
                        interval_seconds=int(plan.get("interval_seconds") or 30),
                        daily_limit=int(plan.get("daily_limit") or 100),
                        auto_start=bool(plan.get("auto_start")),
                        source="PROSPECTING",
                    )
                    plan = {
                        **plan,
                        "dispatch_id": dispatch.id if dispatch else None,
                        "dispatch_summary": summary,
                    }
                    _set_execution_plan(job, plan)
                else:
                    plan = {
                        **plan,
                        "dispatch_error": (
                            "Campanha ou fila inválida para montar o disparo."
                        ),
                    }
                    _set_execution_plan(job, plan)

            job.status = "CONCLUIDO"
            LOG.info(
                "prospecting_job_done job=%s found=%s imported=%s dispatch=%s",
                job.id,
                job.found_count,
                job.imported_count,
                _execution_plan(job).get("dispatch_id"),
            )
        except Exception as exc:
            job.status = "FALHOU"
            job.error = str(exc)[:1000]
            LOG.warning(
                "prospecting_job_failed job=%s type=%s error=%s",
                job.id,
                type(exc).__name__,
                str(exc)[:300],
            )
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
    job_plans = {job.id: _execution_plan(job) for job in jobs}
    can_dispatch = _can_schedule_dispatch(user)
    campaigns = _whatsapp_campaigns(db) if can_dispatch else []
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
        job_plans=job_plans,
        campaigns=campaigns,
        can_schedule_dispatch=can_dispatch,
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
    execution_mode: str = Form(SEARCH_ONLY),
    campaign_id: int | None = Form(None),
    interval_seconds: int = Form(30),
    daily_limit: int = Form(100),
    auto_start_dispatch: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)

    mode = (execution_mode or SEARCH_ONLY).strip().upper()
    if mode not in {SEARCH_ONLY, SEARCH_AND_DISPATCH}:
        mode = SEARCH_ONLY
    if mode == SEARCH_AND_DISPATCH and not _can_schedule_dispatch(user):
        flash(request, "Somente gestores podem agendar disparo na prospecção.", "warning")
        return RedirectResponse("/prospecting", status_code=303)

    selected_queue = get_queue(db, queue_id)
    if queue_id and not selected_queue:
        flash(request, "A fila selecionada não está ativa.", "warning")
        return RedirectResponse("/prospecting", status_code=303)
    if selected_queue and selected_queue.id not in {
        item.id for item in available_queues_for_user(db, user)
    }:
        flash(request, "Você não possui acesso a esta fila.", "error")
        return RedirectResponse("/prospecting", status_code=303)

    campaign = None
    interval = max(5, min(int(interval_seconds or 30), 3600))
    day_limit = max(1, min(int(daily_limit or 100), 5000))
    auto_start = bool(auto_start_dispatch)

    if mode == SEARCH_AND_DISPATCH:
        if not selected_queue:
            flash(
                request,
                "Para buscar e disparar, selecione a fila que receberá as respostas.",
                "warning",
            )
            return RedirectResponse("/prospecting", status_code=303)
        campaign = db.get(Campaign, int(campaign_id or 0))
        if (
            not campaign
            or campaign.channel != "WHATSAPP"
            or campaign.status not in {"APROVADA", "ATIVA", "PAUSADA"}
        ):
            flash(request, "Selecione uma campanha WhatsApp aprovada.", "warning")
            return RedirectResponse("/prospecting", status_code=303)
        if not campaign_message_template(campaign):
            flash(request, "A campanha selecionada não possui mensagem válida.", "warning")
            return RedirectResponse("/prospecting", status_code=303)

    plan: dict[str, Any] = {"mode": mode}
    if mode == SEARCH_AND_DISPATCH and campaign and selected_queue:
        plan.update(
            {
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "queue_id": selected_queue.id,
                "interval_seconds": interval,
                "daily_limit": day_limit,
                "auto_start": auto_start,
            }
        )

    job = ProspectingJob(
        segment=segment.strip(),
        city=city.strip(),
        limit=min(max(limit, 1), 100),
        team=team,
        branch_id=branch_id if user.role in {"ADMIN", "GESTOR"} else user.branch_id,
        created_by_id=user.id,
        status="PENDENTE",
        execution_json=json.dumps(plan, ensure_ascii=False),
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
            "execution": plan,
        },
    )
    db.commit()
    background_tasks.add_task(_run_job, job.id)
    if mode == SEARCH_AND_DISPATCH:
        if auto_start:
            flash(
                request,
                "Busca criada. Ao terminar a importação, o disparo WhatsApp inicia automaticamente.",
            )
        else:
            flash(
                request,
                "Busca criada. Ao terminar, o disparo ficará pronto em Disparos para você iniciar.",
            )
    else:
        flash(request, "Busca criada. O processamento será executado pelo servidor.")
    return RedirectResponse("/prospecting", status_code=303)
