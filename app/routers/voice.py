"""Prospecta VOZ: configuracao e controle de campanhas reais via worker isolado."""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, RedirectResponse

from app.database import get_db
from app.models import Lead, VoiceCampaign, VoiceDialerEngine, VoiceGroup, VoiceRecipient, VoiceSipAccount, VoiceTestCall, VoiceTransferTest
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.dispatch_queue import parse_dispatch_csv
from app.services.media_storage import MediaStorage
from app.services.voice_reporting import campaign_operational_report
from app.services.secret_store import encrypt_secret
from app.services.voice_runtime import (
    check_campaign_ready, validate_routing_options, outbound_guard, make_dial_uri,
    MAX_CALLS_PER_DAY, ENGINE_HEARTBEAT_MAX
)
from app.services.voice_test import aware
from datetime import datetime, timezone, timedelta
from app.services.voice_test import is_agent_online, now_utc, validate_test_phone, verify_test_allowed
from app.services.voice import (
    VOICE_STATUS_HOMOLOGACAO,
    normalize_voice_contacts,
    validate_campaign,
    validate_extension,
    validate_sip,
    validate_wav,
)
from app.web import flash, render

router = APIRouter(prefix="/voice")

# Campanhas ja pausadas podem ter uma chamada encerrando em paralelo.
# Nunca modificar/arquivar enquanto houver ligacao SIP em andamento.
VOICE_CAMPAIGN_EDITABLE = {"RASCUNHO", "PRONTA", VOICE_STATUS_HOMOLOGACAO, "PAUSADA", "CONCLUIDA"}
VOICE_CALL_IN_FLIGHT = ("INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO")
VOICE_CAMPAIGN_ARCHIVED = "EXCLUIDA"


def _campaign_calls_in_flight(db: Session, campaign_id: int) -> bool:
    return bool(db.scalar(select(VoiceRecipient.id).where(
        VoiceRecipient.campaign_id == campaign_id,
        VoiceRecipient.status.in_(VOICE_CALL_IN_FLIGHT),
    ).limit(1)))


def _actor(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    # Centralizar o RBAC: TODOS os GETs e POSTs do VOZ exigem ADMIN.
    # Gestores, supervisores e consultores nao acessam painel nem mutacoes.
    if user.role != "ADMIN":
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _error(request: Request, msg: str, destination: str = "/voice"):
    flash(request, msg, "warning")
    if destination == "/voice":
        path = request.url.path
        if path.startswith("/voice/accounts"):
            destination = "/voice/accounts"
        elif path.startswith("/voice/groups"):
            destination = "/voice/groups"
        elif path == "/voice/campaigns":
            destination = "/voice/campaigns/new"
        elif path.startswith("/voice/campaigns/"):
            destination = "/voice/campaigns"
    return RedirectResponse(destination, status_code=303)


@router.get("")
def voice_home(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    accounts = list(db.scalars(select(VoiceSipAccount).order_by(VoiceSipAccount.id.desc())))
    groups = list(db.scalars(select(VoiceGroup).order_by(VoiceGroup.id.desc())))
    campaigns = list(db.scalars(select(VoiceCampaign).where(
        VoiceCampaign.status != VOICE_CAMPAIGN_ARCHIVED
    ).order_by(VoiceCampaign.id.desc())))
    engine = db.get(VoiceDialerEngine, 1)
    recipient_count = db.scalar(select(func.count(VoiceRecipient.id)).join(
        VoiceCampaign, VoiceRecipient.campaign_id == VoiceCampaign.id
    ).where(VoiceCampaign.status != VOICE_CAMPAIGN_ARCHIVED)) or 0
    stats = {
        "accounts": len(accounts),
        "accounts_active": sum(1 for item in accounts if item.active),
        "accounts_paused": sum(1 for item in accounts if not item.active),
        "groups": len(groups),
        "groups_active": sum(1 for item in groups if item.active),
        "campaigns": len(campaigns),
        "campaigns_draft": sum(1 for item in campaigns if item.status == "RASCUNHO"),
        "campaigns_review": sum(1 for item in campaigns if item.status in {VOICE_STATUS_HOMOLOGACAO, "PRONTA"}),
        "campaigns_live": sum(1 for item in campaigns if item.status == "EM_EXECUCAO"),
        "recipients": recipient_count,
    }
    return render(
        request, "voice/dashboard.html", user=actor, active_nav="voice",
        voice_section="dashboard", page_title="Prospecta VOZ · Dashboard",
        accounts=accounts, campaigns=campaigns[:5], stats=stats, engine=engine,
        engine_online=bool(engine and engine.heartbeat_at and
            datetime.now(timezone.utc) - aware(engine.heartbeat_at) <= ENGINE_HEARTBEAT_MAX),
    )


@router.get("/accounts")
def voice_accounts(request: Request, edit_id: int | None = None, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    accounts = list(db.scalars(select(VoiceSipAccount).order_by(VoiceSipAccount.id.desc())))
    edit_account = db.get(VoiceSipAccount, edit_id) if edit_id else None
    recent = list(db.scalars(select(VoiceTestCall).order_by(VoiceTestCall.id.desc()).limit(150)))
    latest_tests = {}
    for test in recent:
        if test.account_id is not None and test.account_id not in latest_tests:
            latest_tests[test.account_id] = test
    return render(
        request, "voice/accounts.html", user=actor, active_nav="voice",
        voice_section="accounts", page_title="Prospecta VOZ · Contas SIP",
        accounts=accounts, edit_account=edit_account,
        latest_tests=latest_tests, agent_online=is_agent_online(db),
    )


@router.get("/groups")
def voice_groups(request: Request, edit_id: int | None = None, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    groups = list(db.scalars(select(VoiceGroup).order_by(VoiceGroup.id.desc())))
    edit_group = db.get(VoiceGroup, edit_id) if edit_id else None
    accounts = list(db.scalars(select(VoiceSipAccount).where(VoiceSipAccount.active.is_(True))))
    transfers = list(db.scalars(select(VoiceTransferTest).order_by(
        VoiceTransferTest.id.desc()).limit(30)))
    engine = db.get(VoiceDialerEngine, 1)
    return render(
        request, "voice/groups.html", user=actor, active_nav="voice",
        voice_section="groups", page_title="Prospecta VOZ · Ramais e grupos",
        groups=groups, edit_group=edit_group, accounts=accounts, engine=engine,
        transfers=transfers,
        engine_online=bool(engine and engine.heartbeat_at and
            datetime.now(timezone.utc) - aware(engine.heartbeat_at) <= ENGINE_HEARTBEAT_MAX),
    )


@router.get("/campaigns/new")
def voice_new_campaign(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    accounts = list(db.scalars(select(VoiceSipAccount).where(VoiceSipAccount.active.is_(True)).order_by(VoiceSipAccount.name)))
    groups = list(db.scalars(select(VoiceGroup).where(VoiceGroup.active.is_(True)).order_by(VoiceGroup.name)))
    return render(
        request, "voice/campaign_new.html", user=actor, active_nav="voice",
        voice_section="new_campaign", page_title="Prospecta VOZ · Nova campanha",
        accounts=accounts, groups=groups,
    )


@router.get("/campaigns/{campaign_id}/edit")
def edit_voice_campaign_page(campaign_id: int, request: Request,
                             db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign:
        return _error(request, "Campanha não encontrada.", "/voice/campaigns")
    if campaign.status not in VOICE_CAMPAIGN_EDITABLE:
        return _error(request, "Pause a campanha antes de editar. Campanhas excluídas não podem ser alteradas.", "/voice/campaigns")
    if _campaign_calls_in_flight(db, campaign.id):
        return _error(request, "Aguarde o encerramento da ligação em andamento antes de editar.", "/voice/campaigns")
    accounts = list(db.scalars(select(VoiceSipAccount).where(
        (VoiceSipAccount.active.is_(True)) | (VoiceSipAccount.id == campaign.sip_account_id)
    ).order_by(VoiceSipAccount.name)))
    groups = list(db.scalars(select(VoiceGroup).where(
        (VoiceGroup.active.is_(True)) | (VoiceGroup.id == campaign.group_id)
    ).order_by(VoiceGroup.name)))
    previous_attempts = db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.attempts > 0,
    )) or 0
    return render(
        request, "voice/campaign_edit.html", user=actor, active_nav="voice",
        voice_section="campaigns", page_title=f"Prospecta VOZ · Editar campanha #{campaign.id}",
        campaign=campaign, accounts=accounts, groups=groups,
        previous_attempts=previous_attempts,
    )


@router.post("/campaigns/{campaign_id}/update")
def update_voice_campaign(campaign_id: int, request: Request,
                          csrf_token: str = Form(...), name: str = Form(...),
                          sip_account_id: int = Form(...), group_id: str = Form(""),
                          mode: str = Form(...), dtmf_digit: str = Form("1"),
                          window_start: str = Form("09:00"), window_end: str = Form("18:00"),
                          daily_limit: int = Form(30), interval_seconds: int = Form(30),
                          dial_format: str = Form("NATIONAL"),
                          db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.id == campaign_id).with_for_update())
    if not campaign:
        return _error(request, "Campanha não encontrada.", "/voice/campaigns")
    if campaign.status not in VOICE_CAMPAIGN_EDITABLE:
        return _error(request, "Campanhas em execução ou excluídas não podem ser editadas.", "/voice/campaigns")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Existe uma ligação ativa. Aguarde sua finalização antes de editar.", "/voice/campaigns")
    account = db.get(VoiceSipAccount, sip_account_id)
    if not account or (not account.active and account.id != campaign.sip_account_id):
        return _error(request, "Selecione uma conta SIP válida e ativa.", f"/voice/campaigns/{campaign_id}/edit")
    raw_group_id = group_id.strip()
    if raw_group_id and not raw_group_id.isdecimal():
        return _error(request, "Grupo de transferência inválido.", f"/voice/campaigns/{campaign_id}/edit")
    selected_group_id = int(raw_group_id) if raw_group_id else None
    group = db.get(VoiceGroup, selected_group_id) if selected_group_id is not None else None
    if selected_group_id is not None and (
        not group or (not group.active and selected_group_id != campaign.group_id)
    ):
        return _error(request, "Selecione um grupo ativo ou mantenha o atual.", f"/voice/campaigns/{campaign_id}/edit")
    if selected_group_id is None and mode.upper() != "AUDIO":
        return _error(request, "Campanhas sem grupo precisam usar Áudio automático.", f"/voice/campaigns/{campaign_id}/edit")
    if not name.strip() or len(name.strip()) > 180:
        return _error(request, "Informe um nome de até 180 caracteres.", f"/voice/campaigns/{campaign_id}/edit")
    if not 1 <= daily_limit <= 300 or not 10 <= interval_seconds <= 3600:
        return _error(request, "Limite diário ou intervalo inválido.", f"/voice/campaigns/{campaign_id}/edit")
    if dial_format not in {"NATIONAL", "E164"}:
        return _error(request, "Formato de discagem SIP inválido.", f"/voice/campaigns/{campaign_id}/edit")
    try:
        clean_mode = validate_campaign(mode, window_start, window_end, dtmf_digit,
                                      campaign.audio_uri or ("PENDENTE" if mode.upper() == "AUDIO" else ""))
    except ValueError as exc:
        return _error(request, str(exc), f"/voice/campaigns/{campaign_id}/edit")
    fields = ("name", "sip_account_id", "group_id", "mode", "dtmf_digit", "window_start",
              "window_end", "daily_limit", "interval_seconds", "dial_format")
    previous = {field: getattr(campaign, field) for field in fields}
    changed = {
        "name": name.strip(), "sip_account_id": account.id, "group_id": selected_group_id,
        "mode": clean_mode, "dtmf_digit": dtmf_digit,
        "window_start": window_start, "window_end": window_end,
        "daily_limit": daily_limit, "interval_seconds": interval_seconds,
        "dial_format": dial_format,
    }
    if all(previous[key] == value for key, value in changed.items()):
        flash(request, "Nenhuma alteração necessária.")
        return RedirectResponse("/voice/campaigns", status_code=303)
    for field, value in changed.items():
        setattr(campaign, field, value)
    if any(previous[key] != changed[key] for key in ("sip_account_id", "group_id", "mode", "dial_format")):
        # Mudancas materiais exigem nova verificacao pelo administrador.
        campaign.legal_basis_verified = False
        campaign.approved_at = None
        campaign.approved_by_id = None
    if campaign.status in {"PRONTA", VOICE_STATUS_HOMOLOGACAO}:
        campaign.status = "RASCUNHO"
    elif campaign.status == "CONCLUIDA":
        campaign.status = "PAUSADA"
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_UPDATED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"before": previous, "after": changed,
                         "requires_new_legal_confirmation": not campaign.legal_basis_verified})
    db.commit()
    flash(request, "Campanha atualizada. Confira o checklist antes de solicitar novas ligações.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.get("/campaigns/{campaign_id}/delete")
def confirm_archive_voice_campaign(campaign_id: int, request: Request,
                                   db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign:
        return _error(request, "Campanha não encontrada.", "/voice/campaigns")
    if campaign.status not in VOICE_CAMPAIGN_EDITABLE and campaign.status != "CONCLUIDA":
        return _error(request, "Pause a campanha antes de excluí-la.", "/voice/campaigns")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Aguarde o encerramento de todas as ligações antes de excluir.", "/voice/campaigns")
    total_calls = db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign_id, VoiceRecipient.attempts > 0
    )) or 0
    return render(
        request, "voice/campaign_delete.html", user=actor, active_nav="voice",
        voice_section="campaigns", page_title=f"Prospecta VOZ · Excluir campanha #{campaign.id}",
        campaign=campaign, total_calls=total_calls,
    )


@router.post("/campaigns/{campaign_id}/delete")
def archive_voice_campaign(campaign_id: int, request: Request,
                           csrf_token: str = Form(...), confirm_value: str = Form(...),
                           db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.id == campaign_id).with_for_update())
    if not campaign:
        return _error(request, "Campanha não encontrada.", "/voice/campaigns")
    if campaign.status not in (VOICE_CAMPAIGN_EDITABLE | {"CONCLUIDA"}):
        return _error(request, "Pause a campanha antes de excluí-la.", "/voice/campaigns")
    if confirm_value.strip() != campaign.name:
        return _error(request, "Digite o nome exato da campanha para confirmar a exclusão.", f"/voice/campaigns/{campaign_id}/delete")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Ainda há uma chamada em andamento. Exclusão bloqueada.", "/voice/campaigns")
    previous_status = campaign.status
    campaign.status = VOICE_CAMPAIGN_ARCHIVED
    campaign.stopped_at = datetime.now(timezone.utc)
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_ARCHIVED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"campaign_name": campaign.name, "previous_status": previous_status,
                         "kept_recipients_and_call_events": True})
    db.commit()
    flash(request, "Campanha excluída da operação. Histórico de ligações preservado; pode ser restaurada.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/restore")
def restore_voice_campaign(campaign_id: int, request: Request,
                           csrf_token: str = Form(...), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.scalar(select(VoiceCampaign).where(VoiceCampaign.id == campaign_id).with_for_update())
    if not campaign or campaign.status != VOICE_CAMPAIGN_ARCHIVED:
        return _error(request, "Campanha não está excluída.", "/voice/campaigns?archived=1")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Não é possível restaurar enquanto há ligação ativa.", "/voice/campaigns?archived=1")
    campaign.status = "RASCUNHO" if campaign.started_at is None else "PAUSADA"
    campaign.legal_basis_verified = False
    campaign.approved_at = None
    campaign.approved_by_id = None
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_RESTORED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"restored_as": campaign.status, "manual_start_required": True})
    db.commit()
    flash(request, "Campanha restaurada. Nova autorização é obrigatória antes de iniciar.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.get("/campaigns")
def voice_campaigns(request: Request, archived: bool = False, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    predicate = (VoiceCampaign.status == VOICE_CAMPAIGN_ARCHIVED if archived
                 else VoiceCampaign.status != VOICE_CAMPAIGN_ARCHIVED)
    campaigns = list(db.scalars(select(VoiceCampaign).where(predicate).order_by(VoiceCampaign.id.desc())))
    archived_count = db.scalar(select(func.count(VoiceCampaign.id)).where(
        VoiceCampaign.status == VOICE_CAMPAIGN_ARCHIVED
    )) or 0
    engine = db.get(VoiceDialerEngine, 1)
    # A listagem só carrega nome/status/contagem: checklist, eventos e métricas
    # são consultados apenas quando o administrador expande uma campanha.
    ids = [item.id for item in campaigns]
    counts = dict(db.execute(select(
        VoiceRecipient.campaign_id, func.count(VoiceRecipient.id),
    ).where(VoiceRecipient.campaign_id.in_(ids)).group_by(
        VoiceRecipient.campaign_id,
    )).all()) if ids else {}
    return render(
        request, "voice/campaigns.html", user=actor, active_nav="voice",
        voice_section="campaigns", page_title="Prospecta VOZ · Campanhas e preparação",
        campaigns=campaigns, counts=counts, engine=engine, show_archived=archived,
        archived_count=archived_count,
        engine_online=bool(engine and engine.heartbeat_at and
            datetime.now(timezone.utc) - aware(engine.heartbeat_at) <= ENGINE_HEARTBEAT_MAX),
    )


@router.get("/campaigns/{campaign_id}/details")
def voice_campaign_details(campaign_id: int, request: Request,
                           db: Session = Depends(get_db)):
    """Fragmento sob demanda: sem modificar campanha nem iniciar ligação."""
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Campanha não encontrada.")
    readiness = check_campaign_ready(db, campaign)
    report = campaign_operational_report(db, campaign, readiness)
    count = db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign.id,
    )) or 0
    recipient = db.scalar(select(VoiceRecipient).where(
        VoiceRecipient.campaign_id == campaign.id,
    ).order_by(VoiceRecipient.id.asc()).limit(1))
    sip_preview = None
    if recipient:
        try:
            uri = make_dial_uri(
                campaign.sip_account, recipient.phone,
                dial_format=campaign.dial_format or "NATIONAL",
            )
            sip_preview = uri.partition("@")[0].removeprefix("sip:")
        except ValueError:
            sip_preview = "Número inválido — revisar CSV"
    response = render(
        request, "voice/_campaign_detail.html", user=actor, active_nav="voice",
        campaign=campaign, count=count, report=report, readiness=readiness,
        sip_preview=sip_preview,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/campaigns/status")
def voice_campaigns_live_status(request: Request, campaign_id: int | None = None,
                                db: Session = Depends(get_db)):
    """Polling de status; com campaign_id consulta só a campanha aberta."""
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    query = select(VoiceCampaign).where(VoiceCampaign.status != VOICE_CAMPAIGN_ARCHIVED)
    if campaign_id is not None:
        query = query.where(VoiceCampaign.id == campaign_id)
    campaigns = db.scalars(query.order_by(VoiceCampaign.id.desc())).all()
    data = {
        str(item.id): campaign_operational_report(db, item, check_campaign_ready(db, item))
        for item in campaigns
    }
    return JSONResponse({"campaigns": data}, headers={"Cache-Control": "no-store"})


@router.get("/accounts/{account_id}/delete")
def confirm_delete_account(account_id: int, request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    campaigns_using = db.scalar(select(func.count(VoiceCampaign.id)).where(VoiceCampaign.sip_account_id == account_id)) or 0
    return render(
        request, "voice/confirm_delete.html", user=actor, active_nav="voice",
        voice_section="accounts", page_title="Prospecta VOZ · Excluir conta",
        item_name=account.name, item_kind="conta SIP", action=f"/voice/accounts/{account_id}/delete",
        back_url="/voice/accounts", dependencies=campaigns_using,
    )


@router.get("/groups/{group_id}/delete")
def confirm_delete_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    group = db.get(VoiceGroup, group_id)
    if not group:
        return _error(request, "Grupo nao encontrado.", "/voice/groups")
    campaigns_using = db.scalar(select(func.count(VoiceCampaign.id)).where(VoiceCampaign.group_id == group_id)) or 0
    return render(
        request, "voice/confirm_delete.html", user=actor, active_nav="voice",
        voice_section="groups", page_title="Prospecta VOZ · Excluir grupo",
        item_name=group.name, item_kind="grupo de ramais", action=f"/voice/groups/{group_id}/delete",
        back_url="/voice/groups", dependencies=campaigns_using,
    )



@router.post("/accounts/{account_id}/update")
def update_account(account_id: int, request: Request, csrf_token: str = Form(...),
                   name: str = Form(...), domain: str = Form(...),
                   username: str = Form(...), transport: str = Form("UDP"),
                   sip_port: int = Form(5060), proxy_host: str = Form(""),
                   proxy_port: int = Form(5060), dial_domain: str = Form(""),
                   dial_prefix: str = Form(""), strip_brazil_ddi: str = Form(""),
                   caller_id: str = Form(""), password: str = Form(""),
                   db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    try:
        host, user, proto = validate_sip(domain, username, transport)
        routing = validate_routing_options(sip_port, proxy_host, proxy_port,
                                           dial_domain, dial_prefix, caller_id)
        if not name.strip() or len(name.strip()) > 120 or len(password) > 300:
            raise ValueError("Nome ou senha invalida.")
    except ValueError as exc:
        return _error(request, str(exc), "/voice/accounts")
    account.name = name.strip()
    account.domain = host
    account.username = user
    account.transport = proto
    for key, value in routing.items():
        setattr(account, key, value)
    account.strip_brazil_ddi = strip_brazil_ddi.lower() in {"on", "true", "yes", "1"}
    # Never reuse a prior SIP status after changing credentials or routing.
    account.sip_check_at = None
    account.sip_check_code = None
    account.sip_check_message = "Configuracao alterada; teste SIP necessario."
    # Transfer results from the previous account configuration cannot certify this one.
    for prior in db.scalars(select(VoiceTransferTest).where(
        VoiceTransferTest.account_id == account.id,
        VoiceTransferTest.result == "TRANSFERIDA",
    )):
        prior.result = "INVALIDADO"
        prior.message = "Conta SIP alterada; novo teste de transferencia necessario."
    if password.strip():
        account.password_encrypted = encrypt_secret(password)
    write_audit(db, user=actor, action="VOICE_SIP_UPDATED", entity_type="voice_sip_account",
                entity_id=account.id, payload={"domain": host, "username": user,
                                               "credential_rotated": bool(password.strip())})
    db.commit()
    flash(request, "Conta SIP atualizada. O registro SIP ainda precisa de verificacao real.")
    return RedirectResponse("/voice/accounts", status_code=303)


@router.post("/accounts/{account_id}/toggle")
def toggle_account(account_id: int, request: Request, csrf_token: str = Form(...),
                   db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    account.active = not account.active
    write_audit(db, user=actor, action="VOICE_SIP_ACTIVATED" if account.active else "VOICE_SIP_PAUSED",
                entity_type="voice_sip_account", entity_id=account.id)
    db.commit()
    flash(request, "Conta ativada para configuracao." if account.active else "Conta pausada para novas campanhas.")
    return RedirectResponse("/voice/accounts", status_code=303)


@router.post("/accounts/{account_id}/delete")
def delete_account(account_id: int, request: Request, csrf_token: str = Form(...),
                   confirm_value: str = Form(...), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    linked = db.scalar(select(func.count(VoiceCampaign.id)).where(VoiceCampaign.sip_account_id == account_id)) or 0
    if linked:
        return _error(request, "Esta conta tem campanhas vinculadas. Pause-a para preservar o historico.", "/voice/accounts")
    if confirm_value.strip() != account.name:
        return _error(request, "Confirme a exclusao digitando exatamente o nome da conta.", f"/voice/accounts/{account_id}/delete")
    db.delete(account)
    write_audit(db, user=actor, action="VOICE_SIP_DELETED", entity_type="voice_sip_account",
                entity_id=account_id)
    db.commit()
    flash(request, "Conta SIP excluida com seguranca.")
    return RedirectResponse("/voice/accounts", status_code=303)


@router.post("/groups/{group_id}/update")
def update_group(group_id: int, request: Request, csrf_token: str = Form(...),
                 name: str = Form(...), target_extension: str = Form(...),
                 member_extensions: str = Form(""), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    group = db.get(VoiceGroup, group_id)
    if not group:
        return _error(request, "Grupo nao encontrado.", "/voice/groups")
    try:
        target = validate_extension(target_extension)
        members = [validate_extension(x) for x in re.split(r"[,;\s]+", member_extensions.strip()) if x]
        if len(members) > 50 or not name.strip() or len(name.strip()) > 120:
            raise ValueError("Grupo invalido (maximo 50 ramais).")
    except ValueError as exc:
        return _error(request, str(exc), "/voice/groups")
    group.name = name.strip()
    group.target_extension = target
    group.member_extensions = ",".join(sorted(set(members)))
    group.transfer_verified_at = None
    for prior in db.scalars(select(VoiceTransferTest).where(
        VoiceTransferTest.group_id == group.id,
        VoiceTransferTest.result == "TRANSFERIDA",
    )):
        prior.result = "INVALIDADO"
        prior.message = "Grupo alterado; transferencia precisa ser retestada."
    write_audit(db, user=actor, action="VOICE_GROUP_UPDATED", entity_type="voice_group",
                entity_id=group.id, payload={"target": target, "members": members})
    db.commit()
    flash(request, "Grupo atualizado. Verifique o destino no PABX antes das chamadas.")
    return RedirectResponse("/voice/groups", status_code=303)


@router.post("/groups/{group_id}/toggle")
def toggle_group(group_id: int, request: Request, csrf_token: str = Form(...),
                 db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    group = db.get(VoiceGroup, group_id)
    if not group:
        return _error(request, "Grupo nao encontrado.", "/voice/groups")
    group.active = not group.active
    write_audit(db, user=actor, action="VOICE_GROUP_ACTIVATED" if group.active else "VOICE_GROUP_PAUSED",
                entity_type="voice_group", entity_id=group.id)
    db.commit()
    flash(request, "Grupo ativado." if group.active else "Grupo pausado para novas campanhas.")
    return RedirectResponse("/voice/groups", status_code=303)


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, csrf_token: str = Form(...),
                 confirm_value: str = Form(...), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    group = db.get(VoiceGroup, group_id)
    if not group:
        return _error(request, "Grupo nao encontrado.", "/voice/groups")
    linked = db.scalar(select(func.count(VoiceCampaign.id)).where(VoiceCampaign.group_id == group_id)) or 0
    if linked:
        return _error(request, "Este grupo tem campanhas vinculadas. Pause-o para preservar o historico.", "/voice/groups")
    if confirm_value.strip() != group.name:
        return _error(request, "Confirme a exclusao digitando exatamente o nome do grupo.", f"/voice/groups/{group_id}/delete")
    db.delete(group)
    write_audit(db, user=actor, action="VOICE_GROUP_DELETED", entity_type="voice_group",
                entity_id=group_id)
    db.commit()
    flash(request, "Grupo excluido com seguranca.")
    return RedirectResponse("/voice/groups", status_code=303)



@router.post("/accounts")
def add_account(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                domain: str = Form(...), username: str = Form(...), password: str = Form(...),
                transport: str = Form("UDP"), sip_port: int = Form(5060),
                proxy_host: str = Form(""), proxy_port: int = Form(5060),
                dial_domain: str = Form(""), dial_prefix: str = Form(""),
                strip_brazil_ddi: str = Form(""), caller_id: str = Form(""),
                db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN")
    try:
        host, user, proto = validate_sip(domain, username, transport)
        routing = validate_routing_options(sip_port, proxy_host, proxy_port,
                                           dial_domain, dial_prefix, caller_id)
        if not password.strip() or len(password) > 300:
            raise ValueError("Informe a senha SIP (maximo 300 caracteres).")
        if not name.strip() or len(name.strip()) > 120:
            raise ValueError("Nome invalido.")
    except ValueError as exc:
        return _error(request, str(exc))
    account = VoiceSipAccount(name=name.strip(), domain=host, username=user,
                              password_encrypted=encrypt_secret(password),
                              transport=proto, created_by_id=actor.id,
                              strip_brazil_ddi=strip_brazil_ddi.lower() in {"on", "true", "yes", "1"},
                              **routing)
    db.add(account)
    db.flush()
    write_audit(db, user=actor, action="VOICE_SIP_CREATED", entity_type="voice_sip_account",
                entity_id=account.id, payload={"domain": host, "username": user})
    db.commit()
    flash(request, "Conta SIP protegida cadastrada. Registro na central ainda nao testado.")
    return RedirectResponse("/voice/accounts", status_code=303)


@router.post("/groups")
def add_group(request: Request, csrf_token: str = Form(...), name: str = Form(...),
              target_extension: str = Form(...), member_extensions: str = Form(""),
              db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    try:
        target = validate_extension(target_extension)
        members = [validate_extension(item) for item in re.split(r"[,;\s]+", member_extensions.strip()) if item]
        if len(members) > 50 or not name.strip() or len(name.strip()) > 120:
            raise ValueError("Grupo invalido (maximo 50 ramais).")
    except ValueError as exc:
        return _error(request, str(exc))
    group = VoiceGroup(name=name.strip(), target_extension=target,
                       member_extensions=",".join(sorted(set(members))))
    db.add(group)
    db.flush()
    write_audit(db, user=actor, action="VOICE_GROUP_CREATED", entity_type="voice_group",
                entity_id=group.id, payload={"target": target, "members": members})
    db.commit()
    flash(request, "Grupo cadastrado. Encaminhamento SIP depende de homologacao do PABX.")
    return RedirectResponse("/voice/groups", status_code=303)


@router.post("/campaigns")
def add_campaign(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                 sip_account_id: int = Form(...), group_id: str = Form(""),
                 mode: str = Form(...), dtmf_digit: str = Form("1"),
                 window_start: str = Form("09:00"), window_end: str = Form("18:00"),
                 daily_limit: int = Form(30), interval_seconds: int = Form(30),
                 dial_format: str = Form("NATIONAL"),
                 db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, sip_account_id)
    if not account or not account.active:
        return _error(request, "Selecione uma conta SIP ativa.")
    raw_group_id = group_id.strip()
    if raw_group_id and not raw_group_id.isdecimal():
        return _error(request, "Grupo de transferência inválido.")
    selected_group_id = int(raw_group_id) if raw_group_id else None
    group = db.get(VoiceGroup, selected_group_id) if selected_group_id is not None else None
    if selected_group_id is not None and (not group or not group.active):
        return _error(request, "O grupo de transferência selecionado está indisponível.")
    if selected_group_id is None and mode.upper() != "AUDIO":
        return _error(request, "Sem transferência, escolha a modalidade Áudio automático.")
    if not name.strip() or len(name.strip()) > 180:
        return _error(request, "Informe o nome da campanha.")
    if not 1 <= daily_limit <= 300 or not 10 <= interval_seconds <= 3600:
        return _error(request, "Limite diario ou intervalo invalido.")
    if dial_format not in {"NATIONAL", "E164"}:
        return _error(request, "Formato de discagem invalido.")
    try:
        # AUDIO pode ser criado como rascunho antes do upload, mas nunca aprovado sem WAV.
        clean_mode = validate_campaign(mode, window_start, window_end, dtmf_digit,
                                      "PENDENTE" if mode.upper() == "AUDIO" else "")
    except ValueError as exc:
        return _error(request, str(exc))
    campaign = VoiceCampaign(name=name.strip(), mode=clean_mode, sip_account_id=account.id,
                             group_id=selected_group_id, dtmf_digit=dtmf_digit, daily_limit=daily_limit,
                             interval_seconds=interval_seconds, dial_format=dial_format,
                             window_start=window_start, window_end=window_end,
                             created_by_id=actor.id,
                             status="RASCUNHO", legal_basis_verified=False)
    db.add(campaign)
    db.flush()
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_CREATED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"mode": clean_mode, "group_id": selected_group_id,
                         "without_transfer": selected_group_id is None,
                         "dial_format": dial_format})
    db.commit()
    flash(request, "Campanha criada. Importe os contatos, anexe o áudio e confira o checklist para iniciar.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/audio")
async def upload_audio(campaign_id: int, request: Request, csrf_token: str = Form(...),
                       audio: UploadFile = File(...), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.mode != "AUDIO" or campaign.status not in {"RASCUNHO", "PAUSADA"}:
        return _error(request, "Pause a campanha antes de alterar o áudio.")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Aguarde a finalização da ligação antes de trocar o áudio.", "/voice/campaigns")
    try:
        storage = MediaStorage()
        if not storage.configured:
            raise ValueError("Configure armazenamento de midia seguro antes do upload.")
        # Limite operacional do armazenamento; sem teto arbitrario de 2 MB / 60 segundos.
        data = await audio.read(storage.max_bytes + 1)
        validate_wav(data, max_bytes=storage.max_bytes)
        uri, _ = storage.upload_campaign_bytes(
            data=data, campaign_id=campaign.id,
            filename=f"voice-{uuid4().hex}.wav", mime_type="audio/wav")
    except ValueError as exc:
        return _error(request, str(exc))
    except Exception:
        # Nao exibir mensagem interna do armazenamento.
        return _error(request, "Falha ao validar ou salvar audio no armazenamento seguro.")
    campaign.audio_uri = uri
    campaign.legal_basis_verified = False
    campaign.approved_at = None
    campaign.approved_by_id = None
    write_audit(db, user=actor, action="VOICE_AUDIO_UPDATED",
                entity_type="voice_campaign", entity_id=campaign.id)
    db.commit()
    flash(request, "Áudio carregado. Verifique o checklist e inicie a campanha quando estiver pronto.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/contacts")
async def import_contacts(campaign_id: int, request: Request,
                          csrf_token: str = Form(...), csv_file: UploadFile = File(...),
                          db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.status not in {"RASCUNHO", "PAUSADA"}:
        return _error(request, "Pause a campanha antes de importar novos contatos.")
    if _campaign_calls_in_flight(db, campaign_id):
        return _error(request, "Aguarde o encerramento da ligação antes de importar.", "/voice/campaigns")
    payload = await csv_file.read(1_000_001)
    if len(payload) > 1_000_000:
        return _error(request, "CSV acima de 1 MB.")
    try:
        rows, summary = parse_dispatch_csv(payload, max_rows=5000)
    except ValueError as exc:
        return _error(request, str(exc))
    phones = [item["phone"] for item in rows]
    blocked = set(db.scalars(select(Lead.phone).where(
        Lead.phone.in_(phones), Lead.do_not_contact.is_(True)
    )))
    existing = set(db.scalars(select(VoiceRecipient.phone).where(
        VoiceRecipient.campaign_id == campaign_id
    )))
    clean, skipped = normalize_voice_contacts(rows, blocked | existing)
    for item in clean:
        lead = db.scalar(select(Lead).where(Lead.phone == item["phone"]).order_by(Lead.id.desc()).limit(1))
        db.add(VoiceRecipient(campaign_id=campaign_id, lead_id=lead.id if lead else None,
                              name=item["name"], phone=item["phone"]))
    if clean:
        # Novos destinatarios exigem nova confirmacao da base legal.
        campaign.legal_basis_verified = False
        campaign.approved_at = None
        campaign.approved_by_id = None
    write_audit(db, user=actor, action="VOICE_CONTACTS_IMPORTED",
                entity_type="voice_campaign", entity_id=campaign_id,
                payload={"accepted": len(clean), "skipped": skipped, "invalid": summary["invalid"]})
    db.commit()
    flash(request, f"{len(clean)} contatos preparados; {skipped} duplicados/bloqueados ignorados. Sem chamadas.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/review")
def request_review(campaign_id: int, request: Request, csrf_token: str = Form(...),
                   db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.status != "RASCUNHO":
        return _error(request, "Campanha nao esta em rascunho.")
    try:
        validate_campaign(campaign.mode, campaign.window_start, campaign.window_end,
                          campaign.dtmf_digit, campaign.audio_uri)
    except ValueError as exc:
        return _error(request, str(exc))
    count = db.scalar(select(func.count(VoiceRecipient.id)).where(
        VoiceRecipient.campaign_id == campaign_id)) or 0
    if not count:
        return _error(request, "Importe pelo menos um contato.")
    campaign.status = VOICE_STATUS_HOMOLOGACAO
    write_audit(db, user=actor, action="VOICE_REVIEW_REQUESTED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"contacts": count})
    db.commit()
    flash(request, "Campanha preparada para homologacao. O discador ainda nao efetua chamadas.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.get("/accounts/{account_id}/test")
def account_test_page(account_id: int, request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    recent = list(db.scalars(select(VoiceTestCall).where(
        VoiceTestCall.account_id == account_id
    ).order_by(VoiceTestCall.id.desc()).limit(8)))
    return render(
        request, "voice/account_test.html", user=actor, active_nav="voice",
        voice_section="accounts", page_title="Prospecta VOZ · Testar ligação SIP",
        account=account, recent=recent, agent_online=is_agent_online(db),
        engine=db.get(VoiceDialerEngine, 1),
        engine_online=bool((eng := db.get(VoiceDialerEngine, 1)) and eng.heartbeat_at
                           and datetime.now(timezone.utc) - aware(eng.heartbeat_at) <= ENGINE_HEARTBEAT_MAX),
        processing=bool(recent and recent[0].status in {
            "PENDENTE", "REGISTRANDO", "REGISTRADO", "CHAMANDO", "TOCANDO",
            "PENDENTE_PJSIP", "REGISTRANDO_PJSIP",
        }),
    )


@router.post("/accounts/{account_id}/test")
def request_account_test(account_id: int, request: Request, csrf_token: str = Form(...),
                         phone: str = Form(...), authorized: str = Form(""),
                         db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    # Lock the account until this request is committed: concurrent tests cannot
    # bypass the global queue/rate limit on PostgreSQL.
    account = db.scalar(select(VoiceSipAccount).where(
        VoiceSipAccount.id == account_id
    ).with_for_update())
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    if authorized != "yes":
        return _error(request, "Confirme que o numero de teste e seu ou esta autorizado.",
                      f"/voice/accounts/{account_id}/test")
    try:
        clean = validate_test_phone(phone)
        verify_test_allowed(db, account, clean)
    except ValueError as exc:
        return _error(request, str(exc), f"/voice/accounts/{account_id}/test")
    test = VoiceTestCall(
        account_id=account.id, account_label=account.name, target_phone=clean,
        status="PENDENTE", message="Teste solicitado; aguardando agente SIP.",
        initiator_user_id=actor.id, created_at=now_utc(),
    )
    db.add(test)
    db.flush()
    write_audit(db, user=actor, action="VOICE_SIP_TEST_REQUESTED",
                entity_type="voice_test_call", entity_id=test.id,
                payload={"account_id": account.id, "target_last4": clean[-4:]})
    db.commit()
    flash(request, "Teste autorizado encaminhado ao agente SIP. Acompanhe o resultado real nesta pagina.")
    return RedirectResponse(f"/voice/accounts/{account_id}/test", status_code=303)


@router.post("/accounts/{account_id}/verify")
def verify_account_registration(account_id: int, request: Request,
                                csrf_token: str = Form(...),
                                db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.scalar(select(VoiceSipAccount).where(
        VoiceSipAccount.id == account_id
    ).with_for_update())
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    try:
        verify_test_allowed(db, account, "SIP_ONLY")
    except ValueError as exc:
        return _error(request, str(exc), f"/voice/accounts/{account_id}/test")
    test = VoiceTestCall(
        account_id=account.id, account_label=account.name, target_phone="SIP_ONLY",
        status="PENDENTE",
        message="Verificacao manual do registro SIP solicitada; nenhuma ligacao sera feita.",
        initiator_user_id=actor.id, created_at=now_utc(),
    )
    db.add(test)
    db.flush()
    write_audit(db, user=actor, action="VOICE_SIP_REGISTER_TEST_REQUESTED",
                entity_type="voice_test_call", entity_id=test.id,
                payload={"account_id": account.id, "operation": "REGISTER_ONLY"})
    db.commit()
    flash(request, "Verificacao SIP iniciada sem discagem. Consulte o resultado abaixo.")
    return RedirectResponse(f"/voice/accounts/{account_id}/test", status_code=303)


@router.post("/engine/toggle")
def voice_engine_toggle(request: Request, csrf_token: str = Form(...),
                        db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    engine = db.scalar(select(VoiceDialerEngine).where(VoiceDialerEngine.id == 1).with_for_update())
    if not engine:
        return _error(request, "Motor de voz nao foi instalado no banco.", "/voice/campaigns")
    if not engine.enabled:
        # The emergency stop must be usable even while the worker is offline.
        if not engine.heartbeat_at or (
            datetime.now(timezone.utc) - aware(engine.heartbeat_at) > ENGINE_HEARTBEAT_MAX
        ):
            return _error(request, "O motor PJSUA2 esta offline; nao pode ser ativado.", "/voice/campaigns")
    engine.enabled = not engine.enabled
    if not engine.enabled:
        for campaign in db.scalars(select(VoiceCampaign).where(
            VoiceCampaign.status == "EM_EXECUCAO"
        )):
            campaign.status = "PAUSADA"
            campaign.stopped_at = datetime.now(timezone.utc)
    write_audit(db, user=actor, action="VOICE_ENGINE_ENABLED" if engine.enabled else "VOICE_ENGINE_STOPPED",
                entity_type="voice_dialer_engine", entity_id=engine.id)
    db.commit()
    flash(request, "Motor de voz habilitado para operacao de producao." if engine.enabled
          else "PARADA GERAL: todas as campanhas foram pausadas.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/legal")
def verify_voice_legal_basis(campaign_id: int, request: Request, csrf_token: str = Form(...),
                             confirmation: str = Form(""), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.status not in {"RASCUNHO", "PRONTA", VOICE_STATUS_HOMOLOGACAO, "PAUSADA"}:
        return _error(request, "Campanha nao permite alterar a declaracao de contato.", "/voice/campaigns")
    if confirmation != "confirmado":
        return _error(request, "Confirme a base legal e a lista de contatos antes de autorizar.", "/voice/campaigns")
    campaign.legal_basis_verified = True
    campaign.approved_by_id = actor.id
    campaign.approved_at = datetime.now(timezone.utc)
    write_audit(db, user=actor, action="VOICE_LEGAL_BASIS_CONFIRMED",
                entity_type="voice_campaign", entity_id=campaign_id,
                payload={"confirmed_by_id": actor.id})
    db.commit()
    flash(request, "Base legal confirmada para esta campanha. Nenhuma chamada foi iniciada.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/start")
def start_voice_campaign(campaign_id: int, request: Request, csrf_token: str = Form(...),
                         authorization: str = Form(""), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.scalar(select(VoiceCampaign).where(
        VoiceCampaign.id == campaign_id
    ).with_for_update())
    if not campaign or campaign.status not in {"RASCUNHO", "PRONTA", VOICE_STATUS_HOMOLOGACAO, "PAUSADA"}:
        return _error(request, "Campanha nao pode ser iniciada neste estado.", "/voice/campaigns")
    if authorization != "sim":
        write_audit(db, user=actor, action="VOICE_CAMPAIGN_START_BLOCKED",
                    entity_type="voice_campaign", entity_id=campaign_id,
                    payload={"reasons": ["Autorização explícita do administrador não confirmada."]})
        db.commit()
        return _error(request, "Campanha não iniciada: confirme a autorização do administrador.", "/voice/campaigns")
    failures = check_campaign_ready(db, campaign)
    if failures:
        # Registra inclusive tentativas rejeitadas: o operador ve o motivo no painel.
        write_audit(db, user=actor, action="VOICE_CAMPAIGN_START_BLOCKED",
                    entity_type="voice_campaign", entity_id=campaign_id,
                    payload={"reasons": failures})
        db.commit()
        return _error(request, "NENHUMA LIGAÇÃO INICIADA: " + " / ".join(failures[:3]),
                      "/voice/campaigns")
    campaign.status = "EM_EXECUCAO"
    campaign.started_at = datetime.now(timezone.utc)
    campaign.stopped_at = None
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_STARTED",
                entity_type="voice_campaign", entity_id=campaign_id,
                payload={"manual_authorization": True, "single_call_at_a_time": True})
    db.commit()
    flash(request, "Início registrado! Campanha em execução; acompanhe ligações e resultados no painel abaixo.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/pause")
def pause_voice_campaign(campaign_id: int, request: Request, csrf_token: str = Form(...),
                         db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.status != "EM_EXECUCAO":
        return _error(request, "Campanha nao esta em execucao.", "/voice/campaigns")
    campaign.status = "PAUSADA"
    campaign.stopped_at = datetime.now(timezone.utc)
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_PAUSED",
                entity_type="voice_campaign", entity_id=campaign_id)
    db.commit()
    flash(request, "Campanha pausada. Nao serao originadas novas chamadas.")
    return RedirectResponse("/voice/campaigns", status_code=303)


@router.post("/groups/{group_id}/test-transfer")
def test_group_transfer(group_id: int, request: Request, csrf_token: str = Form(...),
                        sip_account_id: int = Form(...), phone: str = Form(...),
                        authorized: str = Form(""), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, sip_account_id)
    group = db.get(VoiceGroup, group_id)
    if not account or not account.active or not group or not group.active:
        return _error(request, "Conta SIP e grupo precisam estar ativos.", "/voice/groups")
    engine = db.get(VoiceDialerEngine, 1)
    at = datetime.now(timezone.utc)
    if not engine or not engine.enabled or not engine.test_runtime_enabled:
        return _error(request, "Habilite o motor PJSUA2 e o modulo de testes de transferencia.", "/voice/groups")
    if not engine.heartbeat_at or at - aware(engine.heartbeat_at) > ENGINE_HEARTBEAT_MAX:
        return _error(request, "Worker SIP de transferencia offline.", "/voice/groups")
    if authorized != "sim":
        return _error(request, "Confirme que o numero pode receber a ligacao de teste.", "/voice/groups")
    try:
        clean = validate_test_phone(phone)
        outbound_guard(db, clean)
    except ValueError as exc:
        return _error(request, str(exc), "/voice/groups")
    recent = list(db.scalars(select(VoiceTransferTest).where(
        VoiceTransferTest.account_id == account.id,
        VoiceTransferTest.created_at >= at - timedelta(hours=1)
    )))
    if len(recent) >= 3 or any(row.result not in {
        "FALHOU", "TRANSFERIDA", "EXPIRADO", "INDETERMINADO", "SEM_ATENDIMENTO",
        "SEM_INTERESSE", "TIMEOUT", "FALHA_TRANSFERENCIA", "INTERROMPIDA"
    } for row in recent):
        return _error(request, "Aguarde o fim de outro teste ou o limite de tres testes/hora.", "/voice/groups")
    row = VoiceTransferTest(
        account_id=account.id, group_id=group.id, phone_last4=clean[-4:],
        phone_encrypted=encrypt_secret(clean), created_by_id=actor.id,
        result="PENDENTE", message="Teste unico solicitado por ADMIN.", created_at=at,
    )
    db.add(row)
    db.flush()
    write_audit(db, user=actor, action="VOICE_TRANSFER_TEST_REQUESTED",
                entity_type="voice_transfer_test", entity_id=row.id,
                payload={"account_id": account.id, "group_id": group.id, "phone_last4": clean[-4:]})
    db.commit()
    flash(request, "Teste de transferencia iniciado. Acompanhe o status abaixo.")
    return RedirectResponse("/voice/groups", status_code=303)


@router.post("/accounts/{account_id}/test-native")
def request_native_sip_test(account_id: int, request: Request,
                            csrf_token: str = Form(...), phone: str = Form(...),
                            authorized: str = Form(""), db: Session = Depends(get_db)):
    """One authorized native SIP/RTP call, no campaign and no transfer."""
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.scalar(select(VoiceSipAccount).where(
        VoiceSipAccount.id == account_id
    ).with_for_update())
    if not account or not account.active:
        return _error(request, "Conta SIP nao encontrada ou pausada.", "/voice/accounts")
    engine = db.get(VoiceDialerEngine, 1)
    now = datetime.now(timezone.utc)
    if not engine or not engine.enabled or not engine.test_runtime_enabled:
        return _error(request, "Motor nativo PJSUA2 ainda esta bloqueado para testes.", f"/voice/accounts/{account_id}/test")
    if not engine.heartbeat_at or now - aware(engine.heartbeat_at) > ENGINE_HEARTBEAT_MAX:
        return _error(request, "Motor PJSUA2 offline.", f"/voice/accounts/{account_id}/test")
    if authorized != "sim":
        return _error(request, "Confirmacao de autorizacao do destino obrigatoria.",
                      f"/voice/accounts/{account_id}/test")
    try:
        clean = validate_test_phone(phone)
        outbound_guard(db, clean)
    except ValueError as exc:
        return _error(request, str(exc), f"/voice/accounts/{account_id}/test")
    recent = list(db.scalars(select(VoiceTestCall).where(
        VoiceTestCall.account_id == account.id,
        VoiceTestCall.created_at >= now - timedelta(hours=1),
        VoiceTestCall.status != "SIP_VALIDADO",
    )))
    in_progress = list(db.scalars(select(VoiceTestCall).where(
        VoiceTestCall.status.in_({"PENDENTE_PJSIP", "REGISTRANDO_PJSIP"})
    )))
    if len(recent) >= 3 or in_progress or any(
        t.target_phone == clean and now - aware(t.created_at) < timedelta(minutes=3)
        for t in recent
    ):
        return _error(request, "Limite de testes atingido ou ha uma chamada em andamento.",
                      f"/voice/accounts/{account_id}/test")
    test = VoiceTestCall(
        account_id=account.id, account_label=account.name, target_phone=clean,
        status="PENDENTE_PJSIP", message="Chamada nativa PJSUA2 autorizada pelo administrador.",
        initiator_user_id=actor.id, created_at=now,
    )
    db.add(test)
    db.flush()
    write_audit(db, user=actor, action="VOICE_NATIVE_TEST_REQUESTED",
                entity_type="voice_test_call", entity_id=test.id,
                payload={"account_id": account.id, "target_last4": clean[-4:]})
    db.commit()
    flash(request, "Teste SIP/RTP solicitado. Acompanhe o resultado real nesta pagina.")
    return RedirectResponse(f"/voice/accounts/{account_id}/test", status_code=303)
