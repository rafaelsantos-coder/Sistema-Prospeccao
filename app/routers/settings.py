from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.constants import ROLES, TEAMS
from app.models import AuditLog, Branch, ServiceQueue, ServiceQueueMember, Target, User
from app.security import get_current_user, hash_password, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.legacy_adapter import legacy_modules_available, rate_status
from app.services.queue_routing import queue_summary
from app.services.zapi import ZApiError
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
    audit_count = len(
        list(
            db.scalars(
                select(AuditLog.id)
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            )
        )
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
            "targets": len(targets),
            "branches": len(branches),
            "audit": audit_count,
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
    queue_stats = {item.id: queue_summary(db, item) for item in queues}
    queue_instances = {
        item.id: queue_instance(db, item.id)
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
        active_zapi_instances=active_zapi_instances,
        routing_strategies=_routing_strategies(),
    )


@router.get("/whatsapp")
def settings_whatsapp_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _settings_actor(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR")

    zapi_instances = list_zapi_instances(db, active_only=False)
    active_zapi_instances = [item for item in zapi_instances if item.active]
    zapi_instance_info: dict[int, dict] = {}
    phone_to_instance_ids: dict[str, list[int]] = {}
    any_connected = False
    any_smartphone_connected = False
    first_error: str | None = None

    for instance in active_zapi_instances:
        info = {
            "connected": False,
            "smartphone_connected": False,
            "error": None,
            "phone_raw": "",
            "phone_label": instance.phone_label or "",
            "device_name": "",
            "is_business": False,
            "duplicate_phone": False,
        }
        try:
            service = service_for_instance(instance)
            current_status = service.status()
            info["connected"] = bool(current_status.get("connected"))
            info["smartphone_connected"] = bool(
                current_status.get("smartphone_connected")
            )
            any_connected = any_connected or info["connected"]
            any_smartphone_connected = (
                any_smartphone_connected or info["smartphone_connected"]
            )
            device_info = refresh_instance_device_info(instance)
            info["phone_raw"] = str(device_info.get("phone_raw") or "")
            info["phone_label"] = (
                str(device_info.get("phone_label") or "")
                or instance.phone_label
                or ""
            )
            info["device_name"] = str(device_info.get("name") or "")
            info["is_business"] = bool(device_info.get("is_business"))
            phone_key = "".join(
                ch for ch in info["phone_raw"] if ch.isdigit()
            )
            if phone_key:
                phone_to_instance_ids.setdefault(phone_key, []).append(instance.id)
        except ZApiError as exc:
            info["error"] = str(exc)
            if first_error is None:
                first_error = str(exc)
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

    return render(
        request,
        "settings_whatsapp.html",
        user=user,
        active_nav="settings",
        settings_section="whatsapp",
        page_title="Números e conexões Z-API",
        zapi_status=zapi_status,
        zapi_instances=zapi_instances,
        active_zapi_instances=active_zapi_instances,
        zapi_instance_info=zapi_instance_info,
        duplicate_instance_ids=duplicate_instance_ids,
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
    created = User(
        name=name.strip(),
        email=clean_email,
        password_hash=password_hash,
        role=role,
        team=team.strip().upper(),
        branch_id=branch_value,
        active=True,
        must_change_password=True,
    )
    db.add(created)
    db.flush()
    write_audit(
        db,
        user=user,
        action="USER_CREATED",
        entity_type="user",
        entity_id=created.id,
        payload={"role": created.role, "team": created.team, "branch_id": created.branch_id},
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
