from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse, Response

from app.database import get_db
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    ServiceQueue,
    User,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.campaign_queue import campaign_message_template, personalize_message
from app.services.chat_reports import format_duration
from app.services.dispatch_queue import (
    dispatch_counts,
    ensure_dispatch_lead,
    estimated_duration_seconds,
    parse_dispatch_csv,
)
from app.services.queue_routing import available_queues_for_user, get_queue
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter(prefix="/dispatches")


def _actor(request: Request, db: Session) -> tuple[User | None, RedirectResponse | None]:
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR"}:
        flash(request, "Somente gestores podem criar e controlar disparos.", "warning")
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _campaigns(db: Session) -> list[Campaign]:
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


def _dispatch_or_404(db: Session, dispatch_id: int) -> CampaignDispatch:
    dispatch = db.scalar(
        select(CampaignDispatch)
        .options(
            selectinload(CampaignDispatch.campaign),
            selectinload(CampaignDispatch.queue),
            selectinload(CampaignDispatch.created_by),
        )
        .where(CampaignDispatch.id == dispatch_id)
    )
    if not dispatch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return dispatch


@router.get("")
def dispatch_list(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect

    dispatches = list(
        db.scalars(
            select(CampaignDispatch)
            .options(
                selectinload(CampaignDispatch.campaign),
                selectinload(CampaignDispatch.queue),
                selectinload(CampaignDispatch.created_by),
            )
            .order_by(CampaignDispatch.created_at.desc())
            .limit(200)
        )
    )
    counts = {item.id: dispatch_counts(db, item.id) for item in dispatches}

    return render(
        request,
        "dispatches/list.html",
        user=actor,
        active_nav="dispatches",
        page_title="Disparos",
        dispatches=dispatches,
        counts=counts,
        campaigns=_campaigns(db),
        service_queues=available_queues_for_user(db, actor),
        format_duration=format_duration,
    )


@router.get("/sample.csv")
def dispatch_sample_csv(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    payload = (
        "nome;telefone\r\n"
        "Empresa Exemplo 1;5555999990001\r\n"
        "Empresa Exemplo 2;5555999990002\r\n"
    ).encode("utf-8-sig")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="modelo-disparo-sulnet.csv"'
        },
    )


@router.post("")
async def create_dispatch(
    request: Request,
    csrf_token: str = Form(...),
    campaign_id: int = Form(...),
    queue_id: int = Form(...),
    interval_seconds: int = Form(30),
    daily_limit: int = Form(100),
    csv_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")

    campaign = db.get(Campaign, campaign_id)
    if (
        not campaign
        or campaign.channel != "WHATSAPP"
        or campaign.status not in {"APROVADA", "ATIVA", "PAUSADA"}
    ):
        flash(request, "Selecione uma campanha WhatsApp aprovada.", "warning")
        return RedirectResponse("/dispatches", status_code=303)

    template = campaign_message_template(campaign)
    if not template:
        flash(request, "A campanha selecionada não possui mensagem válida.", "warning")
        return RedirectResponse("/dispatches", status_code=303)

    queue = get_queue(db, queue_id)
    allowed_queue_ids = {item.id for item in available_queues_for_user(db, actor)}
    if not queue or queue.id not in allowed_queue_ids:
        flash(request, "Selecione uma fila ativa e permitida.", "warning")
        return RedirectResponse("/dispatches", status_code=303)

    interval = max(5, min(int(interval_seconds or 30), 3600))
    limit = max(1, min(int(daily_limit or campaign.daily_limit or 100), 5000))

    filename = (csv_file.filename or "").lower()
    if filename and not filename.endswith(".csv"):
        flash(request, "Envie um arquivo no formato CSV.", "warning")
        return RedirectResponse("/dispatches", status_code=303)

    raw = await csv_file.read()
    if len(raw) > 8 * 1024 * 1024:
        flash(request, "O CSV excede o limite de 8 MB.", "warning")
        return RedirectResponse("/dispatches", status_code=303)

    try:
        rows, summary = parse_dispatch_csv(raw)
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/dispatches", status_code=303)

    dispatch = CampaignDispatch(
        campaign_id=campaign.id,
        queue_id=queue.id,
        status="RASCUNHO",
        interval_seconds=interval,
        daily_limit=limit,
        created_by_id=actor.id,
    )
    db.add(dispatch)
    db.flush()

    created = 0
    skipped_blocked = 0
    for row in rows:
        lead = ensure_dispatch_lead(
            db,
            name=row["name"],
            phone=row["phone"],
            queue=queue,
            created_by_id=actor.id,
        )
        if lead.do_not_contact or lead.stage in {"NAO_CONTATAR", "PERDIDO", "GANHO"}:
            skipped_blocked += 1
            continue

        message = personalize_message(template, lead)
        if not message:
            continue
        db.add(
            CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                lead_id=lead.id,
                name=row["name"],
                phone=row["phone"],
                message_body=message,
                status="PENDENTE",
            )
        )
        created += 1

    summary["imported"] = created
    summary["blocked"] = skipped_blocked
    summary["estimated_duration_seconds"] = estimated_duration_seconds(
        created,
        interval,
    )
    dispatch.import_summary_json = json.dumps(summary, ensure_ascii=False)

    if created == 0:
        db.rollback()
        flash(
            request,
            "Nenhum contato apto para disparo após validar bloqueios e duplicidades.",
            "warning",
        )
        return RedirectResponse("/dispatches", status_code=303)

    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_CREATED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
        payload={
            "campaign_id": campaign.id,
            "queue_id": queue.id,
            "interval_seconds": interval,
            "daily_limit": limit,
            "csv_filename": csv_file.filename,
            "summary": summary,
        },
    )
    db.commit()
    flash(
        request,
        f"Disparo #{dispatch.id} preparado com {created} contato(s). Revise e clique em Iniciar.",
    )
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


@router.get("/{dispatch_id}")
def dispatch_detail(
    dispatch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect

    dispatch = _dispatch_or_404(db, dispatch_id)
    recipients = list(
        db.scalars(
            select(CampaignDispatchRecipient)
            .where(CampaignDispatchRecipient.dispatch_id == dispatch.id)
            .order_by(CampaignDispatchRecipient.id.asc())
            .limit(500)
        )
    )
    counts = dispatch_counts(db, dispatch.id)
    try:
        import_summary = json.loads(dispatch.import_summary_json or "{}")
    except (TypeError, ValueError):
        import_summary = {}

    return render(
        request,
        "dispatches/detail.html",
        user=actor,
        active_nav="dispatches",
        page_title=f"Disparo #{dispatch.id}",
        dispatch=dispatch,
        recipients=recipients,
        counts=counts,
        import_summary=import_summary,
        estimated_duration=format_duration(
            estimated_duration_seconds(
                counts["total"],
                dispatch.interval_seconds,
            )
        ),
    )


@router.post("/{dispatch_id}/start")
def start_dispatch(
    dispatch_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    counts = dispatch_counts(db, dispatch.id)
    if counts["pending"] <= 0:
        flash(request, "Este disparo não possui contatos pendentes.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    dispatch.status = "ATIVO"
    dispatch.started_at = dispatch.started_at or utcnow()
    dispatch.paused_at = None
    dispatch.next_send_at = utcnow()
    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_STARTED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
        payload={
            "campaign_id": dispatch.campaign_id,
            "queue_id": dispatch.queue_id,
            "interval_seconds": dispatch.interval_seconds,
            "pending": counts["pending"],
        },
    )
    db.commit()
    flash(request, "Disparo iniciado. O worker enviará as mensagens no intervalo configurado.")
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


@router.post("/{dispatch_id}/pause")
def pause_dispatch(
    dispatch_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    dispatch.status = "PAUSADO"
    dispatch.paused_at = utcnow()
    dispatch.next_send_at = None
    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_PAUSED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
    )
    db.commit()
    flash(request, "Disparo pausado.")
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


@router.post("/{dispatch_id}/cancel")
def cancel_dispatch(
    dispatch_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    if dispatch.status == "CONCLUIDO":
        flash(request, "Este disparo já foi concluído.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    dispatch.status = "CANCELADO"
    dispatch.finished_at = utcnow()
    dispatch.next_send_at = None
    for recipient in db.scalars(
        select(CampaignDispatchRecipient).where(
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
            CampaignDispatchRecipient.status.in_({"PENDENTE", "ENVIANDO"}),
        )
    ):
        recipient.status = "IGNORADO"
        recipient.error = "Disparo cancelado pelo gestor."

    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_CANCELLED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
    )
    db.commit()
    flash(request, "Disparo cancelado.")
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)
