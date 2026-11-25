from __future__ import annotations

import json
import secrets
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.constants import ROLES, TEAMS
from app.models import AuditLog, Branch, OriginCampaign, ServiceQueue, ServiceQueueMember, Target, User
from app.security import get_current_user, hash_password, require_permission, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.access_control import (
    effective_permissions,
    has_permission,
    permission_catalog,
    role_default_permissions,
    validate_selection,
)
from app.services.origin_campaigns import list_campaigns, validate_campaign_fields
from app.services.legacy_adapter import legacy_modules_available, rate_status
from app.services.queue_routing import queue_summary
from app.services.zapi import ZApiError
from app.timeutils import utcnow
from app.services.meta_whatsapp import (
    account_webhook_url,
    list_meta_accounts,
    queue_meta_account,
)
from app.services.zapi_instances import (
    list_zapi_instances,
    queue_instance,
    refresh_instance_device_info,
    service_for_instance,
)
from app.web import flash, render


router = APIRouter(prefix="/settings")


def _temporary_password(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.isupper() for ch in password)
            and any(ch.islower() for ch in password)
            and any(ch.isdigit() for ch in password)
            and any(ch in "!@#$%" for ch in password)
        ):
            return password


def _editable_user_or_404(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.role == "AI_SDR":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if actor.role != "ADMIN" and target.role in {"ADMIN", "GESTOR"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return target


def _render_user_edit(
    request: Request,
    db: Session,
    actor: User,
    target: User,
    *,
    temporary_password: str | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "user_edit.html",
        user=actor,
        active_nav="settings",
        settings_section="users",
        page_title=f"Editar usuário · {target.name}",
        target_user=target,
        branches=list(db.scalars(select(Branch).order_by(Branch.city))),
        access_groups=permission_catalog(actor, target.role),
        access_selected=sorted(effective_permissions(target)),
        access_presets={
            role: sorted(role_default_permissions(role))
            for role in ("CONSULTOR", "SUPERVISOR", "GESTOR", "ADMIN")
        },
        available_user_queues=[q for q in _visible_queues(db, actor) if q.active],
        selected_queue_ids=set(db.scalars(
            select(ServiceQueueMember.queue_id).where(
                ServiceQueueMember.user_id == target.id,
                ServiceQueueMember.active.is_(True),
            )
        )),
        temporary_password=temporary_password,
        status_code=status_code,
    )


def _settings_actor(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _visible_queues(db: Session, user: User) -> list[ServiceQueue]:
    queues = list(
        db.scalars(
            select(ServiceQueue)
            .options(
                selectinload(ServiceQueue.branch),
                selectinload(ServiceQueue.members).selectinload(ServiceQueueMember.user),
            )
            .order_by(ServiceQueue.name)
        ).unique()
    )
    if user.role == "SUPERVISOR":
        if user.branch_id:
            return [
                item for item in queues
                if item.branch_id in {None, user.branch_id}
            ]
        return [item for item in queues if item.team == user.team]
    return queues


def _validated_user_queue_ids(db: Session, actor: User, selected: list[int]) -> set[int]:
    """Não permitir adicionar colaborador a fila fora do escopo do gestor."""
    require_permission(actor, "settings.queues.manage")
    if len(selected) > 300:
        raise HTTPException(status_code=400, detail="Muitas filas selecionadas.")
    available = {q.id for q in _visible_queues(db, actor) if q.active}
    requested = set(selected)
    if not requested <= available:
        raise HTTPException(status_code=403, detail="Fila não permitida para este gestor.")
    return requested


def _sync_user_queue_memberships(
    db: Session, target: User, actor: User, selected: set[int],
) -> None:
    available = {q.id for q in _visible_queues(db, actor) if q.active}
    current = {
        link.queue_id: link
        for link in db.scalars(
            select(ServiceQueueMember).where(ServiceQueueMember.user_id == target.id)
        )
    }
    for queue_id in available:
        link = current.get(queue_id)
        selected_now = queue_id in selected
        if link:
            link.active = selected_now
        elif selected_now:
            db.add(ServiceQueueMember(
                queue_id=queue_id, user_id=target.id,
                active=True, priority=100,
            ))


def _routing_strategies() -> dict[str, str]:
    return {
        "ROUND_ROBIN": "Carrossel / rodízio",
        "LEAST_OPEN": "Menor número de atendimentos pendentes",
        "MANUAL": "Somente distribuição manual",
    }


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    users = list(db.scalars(select(User).order_by(User.name)))
    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    queues = _visible_queues(db, user)
    targets = list(db.scalars(select(Target).order_by(Target.team)))
    zapi_instances = (
        list_zapi_instances(db, active_only=False)
        if user.role in {"ADMIN", "GESTOR"}
        else []
    )
    meta_accounts = (
        list_meta_accounts(db, active_only=False)
        if user.role in {"ADMIN", "GESTOR"}
        else []
    )
    audit_count = len(
        list(
            db.scalars(
                select(AuditLog.id)
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            )
        )
    )
    origin_campaigns_count = (
        db.scalar(
            select(func.count(OriginCampaign.id)).where(OriginCampaign.active.is_(True))
        )
        or 0
    )

    return render(
        request,
        "settings.html",
        user=user,
        active_nav="settings",
        page_title="Configurações",
        settings_stats={
            "users": len(users),
            "queues": len(queues),
            "zapi_instances": len(zapi_instances),
            "meta_accounts": len(meta_accounts),
            "whatsapp_connections": len(zapi_instances) + len(meta_accounts),
            "targets": len(targets),
            "branches": len(branches),
            "audit": audit_count,
            "origin_campaigns": origin_campaigns_count,
        },
        environment=settings.app_env,
    )


@router.get("/users")
def settings_users_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    users = list(db.scalars(select(User).order_by(User.name)))
    return render(
        request,
        "settings_users.html",
        user=user,
        active_nav="settings",
        settings_section="users",
        page_title="Usuários e acessos",
        branches=branches,
        users=users,
        access_groups=permission_catalog(user, "CONSULTOR"),
        access_selected=sorted(role_default_permissions("CONSULTOR")),
        access_presets={
            role: sorted(role_default_permissions(role))
            for role in ("CONSULTOR", "SUPERVISOR", "GESTOR", "ADMIN")
        },
        available_user_queues=[q for q in _visible_queues(db, user) if q.active],
        selected_queue_ids=set(),
    )


@router.get("/queues")
def settings_queues_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    users = list(db.scalars(select(User).order_by(User.name)))
    human_users = [item for item in users if item.role != "AI_SDR"]
    queues = _visible_queues(db, user)
    active_zapi_instances = list_zapi_instances(db)
    active_meta_accounts = list_meta_accounts(db)
    queue_stats = {item.id: queue_summary(db, item) for item in queues}
    queue_instances = {
        item.id: queue_instance(db, item.id)
        for item in queues
    }
    queue_meta_accounts = {
        item.id: queue_meta_account(db, item.id)
        for item in queues
    }

    return render(
        request,
        "settings_queues.html",
        user=user,
        active_nav="settings",
        settings_section="queues",
        page_title="Filas de atendimento",
        branches=branches,
        human_users=human_users,
        queues=queues,
        queue_stats=queue_stats,
        queue_instances=queue_instances,
        queue_meta_accounts=queue_meta_accounts,
        active_zapi_instances=active_zapi_instances,
        active_meta_accounts=active_meta_accounts,
        routing_strategies=_routing_strategies(),
    )


@router.get("/whatsapp")
def settings_whatsapp_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR")

    # Registros arquivados preservam o historico, mas nao aparecem como
    # instancias disponiveis para configurar filas ou enviar mensagens.
    zapi_instances = list_zapi_instances(db, active_only=True)
    active_zapi_instances = zapi_instances
    zapi_instance_info: dict[int, dict] = {}
    phone_to_instance_ids: dict[str, list[int]] = {}
    any_connected = False
    any_smartphone_connected = False
    first_error: str | None = None

    for instance in active_zapi_instances:
        info = {
            "connected": None,
            "smartphone_connected": False,
            "error": None,
            "device_error": None,
            "status_source": None,
            "phone_raw": "",
            "phone_label": instance.phone_label or "",
            "device_name": "",
            "is_business": False,
            "duplicate_phone": False,
        }
        service = service_for_instance(instance)
        try:
            current_status = service.status()
            info["connected"] = bool(current_status.get("connected"))
            info["smartphone_connected"] = bool(
                current_status.get("smartphone_connected")
            )
            info["status_source"] = "api"
        except ZApiError as exc:
            info["error"] = str(exc)
            if first_error is None:
                first_error = str(exc)
            # Uma falha na consulta nao equivale a desconexao.
            # Callback recente e confirmado pode atestar a ultima conexao,
            # mas um evento antigo nao justifica status online.
            event = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "ZAPI_CONNECTION_EVENT",
                    AuditLog.entity_type == "zapi_instance",
                    AuditLog.entity_id == str(instance.id),
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(1)
            )
            if event is not None:
                created_at = event.created_at
                if created_at.tzinfo is not None:
                    created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
                age = utcnow() - created_at
                if timedelta(0) <= age <= timedelta(minutes=15):
                    try:
                        event_payload = json.loads(event.payload_json or "{}")
                    except (ValueError, TypeError):
                        event_payload = {}
                    if type(event_payload.get("connected")) is bool:
                        info["connected"] = event_payload["connected"]
                        info["status_source"] = "webhook"

        # A consulta de dispositivo e independente da consulta de status.
        # Mesmo se /device falhar, nao sobrescrever um status ja confirmado.
        try:
            device_info = refresh_instance_device_info(instance)
            info["phone_raw"] = str(device_info.get("phone_raw") or "")
            info["phone_label"] = (
                str(device_info.get("phone_label") or "")
                or instance.phone_label
                or ""
            )
            info["device_name"] = str(device_info.get("name") or "")
            info["is_business"] = bool(device_info.get("is_business"))
        except ZApiError as exc:
            info["device_error"] = str(exc)
            if first_error is None:
                first_error = str(exc)
        phone_key = "".join(
            ch for ch in (info["phone_raw"] or info["phone_label"]) if ch.isdigit()
        )
        if phone_key:
            phone_to_instance_ids.setdefault(phone_key, []).append(instance.id)
        any_connected = any_connected or info["connected"] is True
        any_smartphone_connected = any_smartphone_connected or info["smartphone_connected"]
        zapi_instance_info[instance.id] = info

    duplicate_instance_ids = {
        instance_id
        for instance_ids in phone_to_instance_ids.values()
        if len(instance_ids) > 1
        for instance_id in instance_ids
    }
    for instance_id in duplicate_instance_ids:
        if instance_id in zapi_instance_info:
            zapi_instance_info[instance_id]["duplicate_phone"] = True

    if active_zapi_instances:
        db.commit()

    zapi_status = {
        "configured": bool(active_zapi_instances),
        "connected": any_connected,
        "smartphone_connected": any_smartphone_connected,
        "error": first_error,
        "webhook_ready": bool(settings.app_public_url and active_zapi_instances),
    }

    meta_accounts = list_meta_accounts(db, active_only=True)
    meta_account_info: dict[int, dict] = {}
    for account in meta_accounts:
        info = {
            "connected": None,
            "error": None,
            "phone_label": account.phone_label or "",
            "verified_name": "",
            "quality_rating": "",
            "webhook_url": account_webhook_url(account),
            "verify_token": account.verify_token,
        }
        try:
            from app.services.meta_whatsapp import service_for_meta_account

            phone_info = service_for_meta_account(account).phone_info()
            info["connected"] = True
            info["verified_name"] = str(phone_info.get("verified_name") or "")
            info["quality_rating"] = str(phone_info.get("quality_rating") or "")
            display = str(phone_info.get("display_phone_number") or "").strip()
            if display:
                info["phone_label"] = display
        except ZApiError as exc:
            info["connected"] = False
            info["error"] = str(exc)
        meta_account_info[account.id] = info

    return render(
        request,
        "settings_whatsapp.html",
        user=user,
        active_nav="settings",
        settings_section="whatsapp",
        page_title="Números WhatsApp",
        zapi_status=zapi_status,
        zapi_instances=zapi_instances,
        active_zapi_instances=active_zapi_instances,
        zapi_instance_info=zapi_instance_info,
        duplicate_instance_ids=duplicate_instance_ids,
        meta_accounts=meta_accounts,
        meta_account_info=meta_account_info,
        app_public_url=settings.app_public_url,
    )


@router.get("/operations")
def settings_operations_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    targets = list(db.scalars(select(Target).order_by(Target.team)))
    return render(
        request,
        "settings_operations.html",
        user=user,
        active_nav="settings",
        settings_section="operations",
        page_title="Metas e unidades",
        branches=branches,
        targets=targets,
    )


@router.get("/integrations")
def settings_integrations_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    integrations = dict(settings.integrations)
    integrations["zapi"] = bool(list_zapi_instances(db))
    return render(
        request,
        "settings_integrations.html",
        user=user,
        active_nav="settings",
        settings_section="integrations",
        page_title="Integrações e ambiente",
        integrations=integrations,
        legacy_available=legacy_modules_available(),
        rate_status=rate_status(),
        environment=settings.app_env,
    )


@router.get("/audit")
def settings_audit_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect

    audit_logs = list(
        db.scalars(
            select(AuditLog)
            .options(selectinload(AuditLog.user))
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    )
    return render(
        request,
        "settings_audit.html",
        user=user,
        active_nav="settings",
        settings_section="audit",
        page_title="Auditoria",
        audit_logs=audit_logs,
    )


@router.post("/users")
def create_user(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    team: str = Form(...),
    branch_id: str = Form(""),
    permissions_configured: str = Form(""),
    permissions: list[str] = Form(default=[]),
    queue_memberships_configured: str = Form(""),
    queue_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    role = role.strip().upper()
    if role not in {"ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if user.role != "ADMIN" and role in {"ADMIN", "GESTOR"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    clean_email = email.strip().lower()
    if db.scalar(select(User).where(User.email == clean_email)):
        flash(request, "Ja existe um usuario com este e-mail.", "warning")
        return RedirectResponse("/settings/users", status_code=303)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/users", status_code=303)
    branch_value = int(branch_id) if branch_id and branch_id.isdigit() else None
    if permissions_configured == "1":
        selected_permissions = validate_selection(user, role, permissions)
    elif user.role == "GESTOR" and user.access_permissions is not None:
        # A API antiga (sem checkboxes) não pode criar usuários com mais
        # acessos do que o gestor possui por meio da herança do perfil.
        eligible = [
            code for code in role_default_permissions(role)
            if has_permission(user, code)
        ]
        selected_permissions = validate_selection(user, role, eligible)
    else:
        selected_permissions = None
    selected_queues = (
        _validated_user_queue_ids(db, user, queue_ids)
        if queue_memberships_configured == "1" else set()
    )
    created = User(
        name=name.strip(),
        email=clean_email,
        password_hash=password_hash,
        role=role,
        team=team.strip().upper(),
        branch_id=branch_value,
        active=True,
        must_change_password=True,
        access_permissions=selected_permissions,
    )
    db.add(created)
    db.flush()
    for selected_queue_id in sorted(selected_queues):
        db.add(ServiceQueueMember(
            queue_id=selected_queue_id, user_id=created.id,
            active=True, priority=100,
        ))
    write_audit(
        db,
        user=user,
        action="USER_CREATED",
        entity_type="user",
        entity_id=created.id,
        payload={
            "role": created.role, "team": created.team,
            "branch_id": created.branch_id,
            "permissions": sorted(effective_permissions(created)),
            "permissions_custom": selected_permissions is not None,
            "queue_ids": sorted(selected_queues),
        },
    )
    db.commit()
    flash(request, f"Usuario {created.name} criado. A troca de senha sera exigida no primeiro acesso.")
    return RedirectResponse("/settings/users", status_code=303)


@router.get("/users/{user_id}/edit")
def edit_user_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = get_current_user(request, db)
    if not actor:
        return RedirectResponse("/login", status_code=303)
    require_roles(actor, "ADMIN", "GESTOR")
    target = _editable_user_or_404(db, actor, user_id)
    return _render_user_edit(request, db, actor, target)


@router.post("/users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    team: str = Form(...),
    branch_id: str = Form(""),
    active: bool = Form(False),
    permissions_configured: str = Form(""),
    permissions: list[str] = Form(default=[]),
    queue_memberships_configured: str = Form(""),
    queue_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    actor = get_current_user(request, db)
    if not actor:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    target = _editable_user_or_404(db, actor, user_id)

    clean_name = name.strip()
    clean_email = email.strip().lower()
    clean_role = role.strip().upper()
    clean_team = team.strip().upper()
    if not clean_name:
        flash(request, "Informe o nome do usuário.", "warning")
        return _render_user_edit(request, db, actor, target, status_code=422)
    if "@" not in clean_email:
        flash(request, "Informe um e-mail válido.", "warning")
        return _render_user_edit(request, db, actor, target, status_code=422)
    duplicate = db.scalar(
        select(User.id).where(
            User.email == clean_email,
            User.id != target.id,
        )
    )
    if duplicate:
        flash(request, "Já existe outro usuário com este e-mail.", "warning")
        return _render_user_edit(request, db, actor, target, status_code=422)

    allowed_roles = {"ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR"}
    if clean_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if actor.role != "ADMIN" and clean_role in {"ADMIN", "GESTOR"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if clean_team not in TEAMS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    selected_queues = (
        _validated_user_queue_ids(db, actor, queue_ids)
        if queue_memberships_configured == "1" else None
    )
    branch_value = int(branch_id) if branch_id and branch_id.isdigit() else None
    if branch_value and not db.get(Branch, branch_value):
        flash(request, "A unidade selecionada não existe.", "warning")
        return _render_user_edit(request, db, actor, target, status_code=422)

    before = {
        "name": target.name,
        "email": target.email,
        "role": target.role,
        "team": target.team,
        "branch_id": target.branch_id,
        "active": target.active,
        "permissions": sorted(effective_permissions(target)),
        "queue_ids": sorted(db.scalars(
            select(ServiceQueueMember.queue_id).where(
                ServiceQueueMember.user_id == target.id,
                ServiceQueueMember.active.is_(True),
            )
        )),
    }
    target.name = clean_name
    target.email = clean_email
    target.team = clean_team
    target.branch_id = branch_value
    if target.id != actor.id:
        target.role = clean_role
        target.active = active
    else:
        target.role = before["role"]
        target.active = True

    if permissions_configured == "1" and target.id != actor.id:
        if target.role == "ADMIN":
            target.access_permissions = None
        else:
            selected_permissions = validate_selection(actor, target.role, permissions)
            if actor.role == "GESTOR":
                # Direitos adicionais concedidos por admin não podem ser
                # apagados por editores que não estão autorizados a concedê-los.
                existing_protected = {
                    code for code in before["permissions"]
                    if not has_permission(actor, code)
                }
                configured = set(json.loads(selected_permissions or "[]"))
                target.access_permissions = json.dumps(
                    sorted(configured | existing_protected), ensure_ascii=False,
                )
            else:
                target.access_permissions = selected_permissions

    if (permissions_configured != "1" and target.id != actor.id
            and target.role != before["role"]
            and actor.role == "GESTOR" and actor.access_permissions is not None):
        # A alteração legada de perfil não expande silenciosamente os direitos
        # além daqueles que o próprio gestor tem autorização para conceder.
        allowed_defaults = [
            code for code in role_default_permissions(target.role)
            if has_permission(actor, code)
        ]
        target.access_permissions = validate_selection(actor, target.role, allowed_defaults)

    if selected_queues is not None and target.id != actor.id:
        _sync_user_queue_memberships(db, target, actor, selected_queues)

    write_audit(
        db,
        user=actor,
        action="USER_UPDATED",
        entity_type="user",
        entity_id=target.id,
        payload={
            "before": before,
            "after": {
                "name": target.name,
                "email": target.email,
                "role": target.role,
                "team": target.team,
                "branch_id": target.branch_id,
                "active": target.active,
                "permissions": sorted(effective_permissions(target)),
                "queue_ids": sorted(
                    selected_queues if selected_queues is not None else before["queue_ids"]
                ),
            },
        },
    )
    db.commit()
    flash(request, f"Usuário {target.name} atualizado.")
    return RedirectResponse(f"/settings/users/{target.id}/edit", status_code=303)


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor = get_current_user(request, db)
    if not actor:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    target = _editable_user_or_404(db, actor, user_id)

    if target.id == actor.id:
        flash(
            request,
            "Para sua própria conta, use a opção Alterar senha no menu do usuário.",
            "warning",
        )
        return RedirectResponse(f"/settings/users/{target.id}/edit", status_code=303)

    temporary_password = _temporary_password()
    target.password_hash = hash_password(temporary_password)
    target.must_change_password = True
    write_audit(
        db,
        user=actor,
        action="USER_PASSWORD_RESET",
        entity_type="user",
        entity_id=target.id,
        payload={"must_change_password": True},
    )
    db.commit()
    response = _render_user_edit(
        request,
        db,
        actor,
        target,
        temporary_password=temporary_password,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/users/{user_id}/toggle")
def toggle_user(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor = get_current_user(request, db)
    if not actor:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.id == actor.id:
        flash(request, "Voce nao pode desativar o proprio acesso.", "warning")
        return RedirectResponse("/settings/users", status_code=303)
    if actor.role != "ADMIN" and target.role in {"ADMIN", "GESTOR"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    target.active = not target.active
    write_audit(
        db,
        user=actor,
        action="USER_ACTIVATED" if target.active else "USER_DEACTIVATED",
        entity_type="user",
        entity_id=target.id,
    )
    db.commit()
    return RedirectResponse("/settings/users", status_code=303)


@router.get("/origin-campaigns")
def settings_origin_campaigns_page(
    request: Request,
    edit: int | None = None,
    db: Session = Depends(get_db),
):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    require_permission(user, "settings.origin_campaigns.view")
    campaigns = list_campaigns(db, include_inactive=True)
    editing = db.get(OriginCampaign, edit) if edit else None
    if edit is not None and editing is None:
        flash(request, "Campanha não encontrada para edição.", "warning")
        return RedirectResponse("/settings/origin-campaigns", status_code=303)
    return render(
        request,
        "settings_origin_campaigns.html",
        user=user,
        active_nav="settings",
        settings_section="origin_campaigns",
        page_title="Campanhas de origem",
        campaigns=campaigns,
        editing_campaign=editing,
        can_manage_origin_campaigns=has_permission(user, "settings.origin_campaigns.manage"),
        settings_stats={"origin_campaigns": len(campaigns)},
    )


@router.post("/origin-campaigns")
def create_origin_campaign(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    match_text: str = Form(...),
    crm_origin: str = Form(...),
    link: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_permission(user, "settings.origin_campaigns.manage")
    try:
        clean_name, clean_match, clean_origin, clean_link = validate_campaign_fields(
            name=name,
            match_text=match_text,
            crm_origin=crm_origin,
            link=link,
        )
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/settings/origin-campaigns", status_code=303)

    now = utcnow()
    campaign = OriginCampaign(
        name=clean_name,
        match_text=clean_match,
        link=clean_link,
        crm_origin=clean_origin,
        active=True,
        created_by_id=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(campaign)
    db.flush()
    write_audit(
        db,
        user=user,
        action="ORIGIN_CAMPAIGN_CREATED",
        entity_type="origin_campaign",
        entity_id=campaign.id,
        payload={"name": clean_name, "crmOrigin": clean_origin},
    )
    db.commit()
    flash(request, "Campanha de origem cadastrada.", "success")
    return RedirectResponse("/settings/origin-campaigns", status_code=303)


@router.post("/origin-campaigns/{campaign_id}/edit")
def update_origin_campaign(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    match_text: str = Form(...),
    crm_origin: str = Form(...),
    link: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_permission(user, "settings.origin_campaigns.manage")
    campaign = db.get(OriginCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        clean_name, clean_match, clean_origin, clean_link = validate_campaign_fields(
            name=name,
            match_text=match_text,
            crm_origin=crm_origin,
            link=link,
        )
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            f"/settings/origin-campaigns?edit={campaign_id}", status_code=303
        )

    campaign.name = clean_name
    campaign.match_text = clean_match
    campaign.crm_origin = clean_origin
    campaign.link = clean_link
    campaign.updated_at = utcnow()
    write_audit(
        db,
        user=user,
        action="ORIGIN_CAMPAIGN_UPDATED",
        entity_type="origin_campaign",
        entity_id=campaign.id,
        payload={"name": clean_name, "crmOrigin": clean_origin},
    )
    db.commit()
    flash(request, "Campanha de origem atualizada.", "success")
    return RedirectResponse("/settings/origin-campaigns", status_code=303)


@router.post("/origin-campaigns/{campaign_id}/toggle")
def toggle_origin_campaign(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_permission(user, "settings.origin_campaigns.manage")
    campaign = db.get(OriginCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    campaign.active = not campaign.active
    campaign.updated_at = utcnow()
    write_audit(
        db,
        user=user,
        action="ORIGIN_CAMPAIGN_ACTIVATED" if campaign.active else "ORIGIN_CAMPAIGN_DEACTIVATED",
        entity_type="origin_campaign",
        entity_id=campaign.id,
        payload={"name": campaign.name, "active": campaign.active},
    )
    db.commit()
    flash(
        request,
        "Campanha reativada." if campaign.active else "Campanha desativada.",
        "success",
    )
    return RedirectResponse("/settings/origin-campaigns", status_code=303)


@router.post("/origin-campaigns/{campaign_id}/delete")
def delete_origin_campaign(
    campaign_id: int,
    request: Request,
    csrf_token: str = Form(...),
    confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_permission(user, "settings.origin_campaigns.manage")
    campaign = db.get(OriginCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if (confirmation or "").strip().upper() != "EXCLUIR":
        flash(request, "Digite EXCLUIR para confirmar a exclusão da campanha.", "warning")
        return RedirectResponse("/settings/origin-campaigns", status_code=303)

    name = campaign.name
    write_audit(
        db,
        user=user,
        action="ORIGIN_CAMPAIGN_DELETED",
        entity_type="origin_campaign",
        entity_id=campaign.id,
        payload={"name": name, "crmOrigin": campaign.crm_origin},
    )
    db.delete(campaign)
    db.commit()
    flash(request, f"Campanha «{name}» excluída.", "success")
    return RedirectResponse("/settings/origin-campaigns", status_code=303)
