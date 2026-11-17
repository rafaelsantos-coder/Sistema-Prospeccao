"""Prospecta VOZ: configuracao e preparacao de campanhas. Nenhum endpoint origina ligacoes."""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Lead, VoiceCampaign, VoiceGroup, VoiceRecipient, VoiceSipAccount, VoiceTestCall
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.dispatch_queue import parse_dispatch_csv
from app.services.media_storage import MediaStorage
from app.services.secret_store import encrypt_secret
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
    campaigns = list(db.scalars(select(VoiceCampaign).order_by(VoiceCampaign.id.desc())))
    recipient_count = db.scalar(select(func.count(VoiceRecipient.id))) or 0
    stats = {
        "accounts": len(accounts),
        "accounts_active": sum(1 for item in accounts if item.active),
        "accounts_paused": sum(1 for item in accounts if not item.active),
        "groups": len(groups),
        "groups_active": sum(1 for item in groups if item.active),
        "campaigns": len(campaigns),
        "campaigns_draft": sum(1 for item in campaigns if item.status == "RASCUNHO"),
        "campaigns_review": sum(1 for item in campaigns if item.status == VOICE_STATUS_HOMOLOGACAO),
        "recipients": recipient_count,
    }
    return render(
        request, "voice/dashboard.html", user=actor, active_nav="voice",
        voice_section="dashboard", page_title="Prospecta VOZ · Dashboard",
        accounts=accounts, campaigns=campaigns[:5], stats=stats,
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
    return render(
        request, "voice/groups.html", user=actor, active_nav="voice",
        voice_section="groups", page_title="Prospecta VOZ · Ramais e grupos",
        groups=groups, edit_group=edit_group,
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


@router.get("/campaigns")
def voice_campaigns(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    campaigns = list(db.scalars(select(VoiceCampaign).order_by(VoiceCampaign.id.desc())))
    counts = dict(db.execute(
        select(VoiceRecipient.campaign_id, func.count(VoiceRecipient.id)).group_by(VoiceRecipient.campaign_id)
    ).all())
    return render(
        request, "voice/campaigns.html", user=actor, active_nav="voice",
        voice_section="campaigns", page_title="Prospecta VOZ · Campanhas e preparação",
        campaigns=campaigns, counts=counts,
    )


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
                   password: str = Form(""), db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    account = db.get(VoiceSipAccount, account_id)
    if not account:
        return _error(request, "Conta SIP nao encontrada.", "/voice/accounts")
    try:
        host, user, proto = validate_sip(domain, username, transport)
        if not name.strip() or len(name.strip()) > 120 or len(password) > 300:
            raise ValueError("Nome ou senha invalida.")
    except ValueError as exc:
        return _error(request, str(exc), "/voice/accounts")
    account.name = name.strip()
    account.domain = host
    account.username = user
    account.transport = proto
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
    return RedirectResponse("/voice/campaigns", status_code=303)


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
        processing=bool(recent and recent[0].status in {"PENDENTE","REGISTRANDO","REGISTRADO","CHAMANDO","TOCANDO"}),
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
