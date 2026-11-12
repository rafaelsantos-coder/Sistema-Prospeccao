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
    parse_window,
)
from app.services.queue_routing import available_queues_for_user, get_queue
from app.services.scoring import normalize_phone
from app.services.telegram import configured as telegram_configured, ensure_telegram_queue
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
        telegram_ready=telegram_configured(),
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
    queue_id: int = Form(0),
    channel: str = Form("WHATSAPP"),
    interval_seconds: int = Form(30),
    daily_limit: int = Form(100),
    window_start: str = Form(""),
    window_end: str = Form(""),
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

    channel = "TELEGRAM" if (channel or "").strip().upper() == "TELEGRAM" else "WHATSAPP"
    if channel == "TELEGRAM":
        # Fila própria do Telegram; só alcança clientes que já abriram o bot.
        if not telegram_configured():
            flash(request, "O bot do Telegram ainda não foi configurado.", "warning")
            return RedirectResponse("/dispatches", status_code=303)
        queue = ensure_telegram_queue(db)
    else:
        queue = get_queue(db, queue_id)
        allowed_queue_ids = {item.id for item in available_queues_for_user(db, actor)}
        if not queue or queue.id not in allowed_queue_ids:
            flash(request, "Selecione uma fila ativa e permitida.", "warning")
            return RedirectResponse("/dispatches", status_code=303)

    interval = max(5, min(int(interval_seconds or 30), 3600))
    limit = max(1, min(int(daily_limit or campaign.daily_limit or 100), 5000))
    try:
        win_start, win_end = parse_window(window_start, window_end)
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/dispatches", status_code=303)

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
        channel=channel,
        status="RASCUNHO",
        interval_seconds=interval,
        daily_limit=limit,
        window_start=win_start,
        window_end=win_end,
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
            "window": [win_start, win_end],
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


@router.post("/{dispatch_id}/configuration")
def update_dispatch_configuration(
    dispatch_id: int,
    request: Request,
    csrf_token: str = Form(...),
    interval_seconds: int = Form(...),
    daily_limit: int = Form(...),
    window_start: str = Form(""),
    window_end: str = Form(""),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    if dispatch.status not in {"RASCUNHO", "PAUSADO"}:
        flash(
            request,
            "Pause o disparo antes de alterar a configuração.",
            "warning",
        )
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    interval = max(5, min(int(interval_seconds or 30), 3600))
    limit = max(1, min(int(daily_limit or 100), 5000))
    old_interval = int(dispatch.interval_seconds or 30)
    old_limit = int(dispatch.daily_limit or 100)
    old_window = [dispatch.window_start, dispatch.window_end]
    try:
        win_start, win_end = parse_window(window_start, window_end)
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    dispatch.interval_seconds = interval
    dispatch.daily_limit = limit
    dispatch.window_start = win_start
    dispatch.window_end = win_end
    if dispatch.status == "PAUSADO":
        dispatch.next_send_at = None

    counts = dispatch_counts(db, dispatch.id)
    try:
        summary = json.loads(dispatch.import_summary_json or "{}")
    except (TypeError, ValueError):
        summary = {}
    summary["estimated_duration_seconds"] = estimated_duration_seconds(
        counts["pending"] + counts["sending"],
        interval,
    )
    dispatch.import_summary_json = json.dumps(summary, ensure_ascii=False)

    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_CONFIG_UPDATED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
        payload={
            "interval_seconds": {
                "from": old_interval,
                "to": interval,
            },
            "daily_limit": {
                "from": old_limit,
                "to": limit,
            },
            "window": {"from": old_window, "to": [win_start, win_end]},
            "status": dispatch.status,
        },
    )
    db.commit()
    janela = f" e janela das {win_start} às {win_end}" if win_start else ""
    flash(
        request,
        f"Configuração atualizada: intervalo de {interval}s, limite de {limit} mensagens/dia{janela}.",
    )
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


@router.post("/{dispatch_id}/recipients/{recipient_id}/edit")
def edit_dispatch_recipient(
    dispatch_id: int,
    recipient_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(""),
    phone: str = Form(...),
    message_body: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    if dispatch.status != "RASCUNHO":
        flash(
            request,
            "A edição dos destinatários só é permitida antes de iniciar o disparo.",
            "warning",
        )
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    recipient = db.scalar(
        select(CampaignDispatchRecipient).where(
            CampaignDispatchRecipient.id == recipient_id,
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
        )
    )
    if not recipient or recipient.status != "PENDENTE":
        flash(request, "Destinatário não disponível para edição.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    clean_phone = normalize_phone(phone)
    if not clean_phone:
        flash(request, "Informe um telefone válido com DDD.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    duplicate = db.scalar(
        select(CampaignDispatchRecipient.id).where(
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
            CampaignDispatchRecipient.phone == clean_phone,
            CampaignDispatchRecipient.id != recipient.id,
        )
    )
    if duplicate:
        flash(request, "Já existe outro destinatário com este telefone no disparo.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    clean_name = name.strip()[:180] or clean_phone
    clean_message = message_body.strip()
    if not clean_message:
        flash(request, "A mensagem do destinatário não pode ficar vazia.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)
    if len(clean_message) > 4096:
        flash(request, "A mensagem deve ter no máximo 4.096 caracteres.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    old_phone = recipient.phone
    old_name = recipient.name
    phone_changed = old_phone != clean_phone

    recipient.name = clean_name
    recipient.phone = clean_phone
    recipient.message_body = clean_message
    recipient.error = None

    if phone_changed:
        recipient.lead_id = None
        recipient.conversation_id = None
        recipient.provider_message_id = None
        recipient.attempts = 0

    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_RECIPIENT_EDITED",
        entity_type="campaign_dispatch_recipient",
        entity_id=recipient.id,
        payload={
            "dispatch_id": dispatch.id,
            "phone_changed": phone_changed,
            "name_changed": old_name != clean_name,
            "message_changed": True,
        },
    )
    db.commit()
    flash(request, f"Destinatário #{recipient.id} atualizado.")
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


@router.post("/{dispatch_id}/recipients/{recipient_id}/remove")
def remove_dispatch_recipient(
    dispatch_id: int,
    recipient_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    if dispatch.status != "RASCUNHO":
        flash(
            request,
            "Só é possível remover destinatários antes de iniciar o disparo.",
            "warning",
        )
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    recipient = db.scalar(
        select(CampaignDispatchRecipient).where(
            CampaignDispatchRecipient.id == recipient_id,
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
        )
    )
    if not recipient or recipient.status != "PENDENTE":
        flash(request, "Destinatário não disponível para remoção.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_RECIPIENT_REMOVED",
        entity_type="campaign_dispatch_recipient",
        entity_id=recipient.id,
        payload={
            "dispatch_id": dispatch.id,
            "phone": recipient.phone,
        },
    )

    try:
        summary = json.loads(dispatch.import_summary_json or "{}")
    except (TypeError, ValueError):
        summary = {}
    summary["removed_manual"] = int(summary.get("removed_manual", 0) or 0) + 1
    dispatch.import_summary_json = json.dumps(summary, ensure_ascii=False)

    db.delete(recipient)
    db.commit()
    flash(request, "Destinatário removido da revisão.")
    return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)


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


CANCEL_REASON = "Disparo cancelado pelo gestor."


@router.post("/{dispatch_id}/reopen")
def reopen_cancelled_dispatch(
    dispatch_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Desfaz um cancelamento: devolve à fila os contatos que o cancelamento ignorou e retoma o envio."""
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    dispatch = _dispatch_or_404(db, dispatch_id)
    if dispatch.status != "CANCELADO":
        flash(request, "Só dá para retomar um disparo cancelado.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    restored = 0
    for recipient in db.scalars(
        select(CampaignDispatchRecipient).where(
            CampaignDispatchRecipient.dispatch_id == dispatch.id,
            CampaignDispatchRecipient.status == "IGNORADO",
            CampaignDispatchRecipient.error == CANCEL_REASON,
        )
    ):
        recipient.status = "PENDENTE"
        recipient.error = None
        restored += 1
    if not restored:
        flash(request, "Não há contatos para retomar neste disparo.", "warning")
        return RedirectResponse(f"/dispatches/{dispatch.id}", status_code=303)

    dispatch.status = "ATIVO"
    dispatch.finished_at = None
    dispatch.paused_at = None
    dispatch.next_send_at = utcnow()
    write_audit(
        db,
        user=actor,
        action="CAMPAIGN_DISPATCH_REOPENED",
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
        payload={"restored": restored},
    )
    db.commit()
    flash(request, f"Disparo retomado: {restored} contatos voltaram para a fila.")
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
        recipient.error = CANCEL_REASON

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
