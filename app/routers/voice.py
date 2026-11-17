"""Prospecta VOZ: configuracao e preparacao de campanhas. Nenhum endpoint origina ligacoes."""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Lead, VoiceCampaign, VoiceGroup, VoiceRecipient, VoiceSipAccount
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.dispatch_queue import parse_dispatch_csv
from app.services.media_storage import MediaStorage
from app.services.secret_store import encrypt_secret
from app.services.voice import (
    normalize_voice_contacts,
    validate_campaign,
    validate_extension,
    validate_sip,
    validate_wav,
)
from app.web import flash, render

router = APIRouter(prefix="/voice")


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
    return RedirectResponse(destination, status_code=303)


@router.get("")
def voice_home(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    accounts = list(db.scalars(select(VoiceSipAccount).order_by(VoiceSipAccount.id.desc())))
    groups = list(db.scalars(select(VoiceGroup).order_by(VoiceGroup.id.desc())))
    campaigns = list(db.scalars(select(VoiceCampaign).order_by(VoiceCampaign.id.desc())))
    counts = dict(db.execute(
        select(VoiceRecipient.campaign_id, func.count(VoiceRecipient.id)).group_by(VoiceRecipient.campaign_id)
    ).all())
    return render(request, "voice/index.html", user=actor, active_nav="voice",
                  page_title="Prospecta VOZ", accounts=accounts, groups=groups,
                  campaigns=campaigns, counts=counts)


@router.post("/accounts")
def add_account(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                domain: str = Form(...), username: str = Form(...), password: str = Form(...),
                transport: str = Form("UDP"), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN")
    try:
        host, user, proto = validate_sip(domain, username, transport)
        if not password.strip() or len(password) > 300:
            raise ValueError("Informe a senha SIP (maximo 300 caracteres).")
        if not name.strip() or len(name.strip()) > 120:
            raise ValueError("Nome invalido.")
    except ValueError as exc:
        return _error(request, str(exc))
    account = VoiceSipAccount(name=name.strip(), domain=host, username=user,
                              password_encrypted=encrypt_secret(password),
                              transport=proto, created_by_id=actor.id)
    db.add(account)
    db.flush()
    write_audit(db, user=actor, action="VOICE_SIP_CREATED", entity_type="voice_sip_account",
                entity_id=account.id, payload={"domain": host, "username": user})
    db.commit()
    flash(request, "Conta SIP protegida cadastrada. Registro na central ainda nao testado.")
    return RedirectResponse("/voice", status_code=303)


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
    return RedirectResponse("/voice", status_code=303)


@router.post("/campaigns")
def add_campaign(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                 sip_account_id: int = Form(...), group_id: int = Form(...),
                 mode: str = Form(...), dtmf_digit: str = Form("1"),
                 window_start: str = Form("09:00"), window_end: str = Form("18:00"),
                 daily_limit: int = Form(30), interval_seconds: int = Form(30),
                 db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, sip_account_id)
    group = db.get(VoiceGroup, group_id)
    if not account or not account.active or not group or not group.active:
        return _error(request, "Selecione conta SIP e grupo ativos.")
    if not name.strip() or len(name.strip()) > 180:
        return _error(request, "Informe o nome da campanha.")
    if not 1 <= daily_limit <= 300 or not 10 <= interval_seconds <= 3600:
        return _error(request, "Limite diario ou intervalo invalido.")
    try:
        # AUDIO pode ser criado como rascunho antes do upload, mas nunca aprovado sem WAV.
        clean_mode = validate_campaign(mode, window_start, window_end, dtmf_digit,
                                      "PENDENTE" if mode.upper() == "AUDIO" else "")
    except ValueError as exc:
        return _error(request, str(exc))
    campaign = VoiceCampaign(name=name.strip(), mode=clean_mode, sip_account_id=account.id,
                             group_id=group.id, dtmf_digit=dtmf_digit, daily_limit=daily_limit,
                             interval_seconds=interval_seconds, window_start=window_start,
                             window_end=window_end, created_by_id=actor.id,
                             status="RASCUNHO", legal_basis_verified=False)
    db.add(campaign)
    db.flush()
    write_audit(db, user=actor, action="VOICE_CAMPAIGN_CREATED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"mode": clean_mode, "group_id": group.id})
    db.commit()
    flash(request, "Campanha de voz criada em rascunho; chamadas reais permanecem bloqueadas.")
    return RedirectResponse("/voice", status_code=303)


@router.post("/campaigns/{campaign_id}/audio")
async def upload_audio(campaign_id: int, request: Request, csrf_token: str = Form(...),
                       audio: UploadFile = File(...), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.mode != "AUDIO" or campaign.status != "RASCUNHO":
        return _error(request, "Campanha nao permite upload de audio.")
    data = await audio.read(2_000_001)
    try:
        validate_wav(data)
        storage = MediaStorage()
        if not storage.configured:
            raise ValueError("Configure armazenamento de midia seguro antes do upload.")
        uri, _ = storage.upload_campaign_bytes(
            data=data, campaign_id=campaign.id,
            filename=f"voice-{uuid4().hex}.wav", mime_type="audio/wav")
    except ValueError as exc:
        return _error(request, str(exc))
    except Exception:
        # Nao exibir mensagem interna do armazenamento.
        return _error(request, "Falha ao validar ou salvar audio no armazenamento seguro.")
    campaign.audio_uri = uri
    write_audit(db, user=actor, action="VOICE_AUDIO_UPDATED",
                entity_type="voice_campaign", entity_id=campaign.id)
    db.commit()
    flash(request, "Audio PCM carregado. Campanha ainda em rascunho.")
    return RedirectResponse("/voice", status_code=303)


@router.post("/campaigns/{campaign_id}/contacts")
async def import_contacts(campaign_id: int, request: Request,
                          csrf_token: str = Form(...), csv_file: UploadFile = File(...),
                          db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    campaign = db.get(VoiceCampaign, campaign_id)
    if not campaign or campaign.status != "RASCUNHO":
        return _error(request, "Importacao permitida somente em campanhas rascunho.")
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
    write_audit(db, user=actor, action="VOICE_CONTACTS_IMPORTED",
                entity_type="voice_campaign", entity_id=campaign_id,
                payload={"accepted": len(clean), "skipped": skipped, "invalid": summary["invalid"]})
    db.commit()
    flash(request, f"{len(clean)} contatos preparados; {skipped} duplicados/bloqueados ignorados. Sem chamadas.")
    return RedirectResponse("/voice", status_code=303)


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
    campaign.status = "AGUARDANDO_HOMOLOGACAO"
    write_audit(db, user=actor, action="VOICE_REVIEW_REQUESTED",
                entity_type="voice_campaign", entity_id=campaign.id,
                payload={"contacts": count})
    db.commit()
    flash(request, "Campanha preparada para homologacao. O discador ainda nao efetua chamadas.")
    return RedirectResponse("/voice", status_code=303)
