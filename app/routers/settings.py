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
        page_title=f"Editar usuário · {target.name}",
        target_user=target,
        branches=list(db.scalars(select(Branch).order_by(Branch.city))),
        temporary_password=temporary_password,
        status_code=status_code,
    )


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return RedirectResponse("/dashboard", status_code=303)
    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    users = list(db.scalars(select(User).order_by(User.name)))
    human_users = [item for item in users if item.role != "AI_SDR"]
    targets = list(db.scalars(select(Target).order_by(Target.team)))
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
            queues = [
                item for item in queues
                if item.branch_id in {None, user.branch_id}
            ]
        else:
            queues = [item for item in queues if item.team == user.team]
    queue_stats = {item.id: queue_summary(db, item) for item in queues}
    audit_logs = list(
        db.scalars(
            select(AuditLog)
            .options(selectinload(AuditLog.user))
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    )
    zapi_instances = list_zapi_instances(db, active_only=False)
    active_zapi_instances = [item for item in zapi_instances if item.active]
    integrations = dict(settings.integrations)
    integrations["zapi"] = bool(active_zapi_instances)
    zapi_status = {
        "configured": bool(active_zapi_instances),
        "connected": False,
        "smartphone_connected": False,
        "error": None,
        "webhook_ready": bool(settings.app_public_url and active_zapi_instances),
    }
    if active_zapi_instances:
        try:
            zapi_status.update(service_for_instance(active_zapi_instances[0]).status())
        except ZApiError as exc:
            zapi_status["error"] = str(exc)
    queue_instances = {
        item.id: queue_instance(db, item.id)
        for item in queues
    }
    return render(
        request,
        "settings.html",
        user=user,
        active_nav="settings",
        page_title="Configurações",
        integrations=integrations,
        zapi_status=zapi_status,
        zapi_instances=zapi_instances,
        active_zapi_instances=active_zapi_instances,
        queue_instances=queue_instances,
        legacy_available=legacy_modules_available(),
        rate_status=rate_status(),
        branches=branches,
        users=users,
        human_users=human_users,
        targets=targets,
        queues=queues,
        queue_stats=queue_stats,
        routing_strategies={
            "ROUND_ROBIN": "Carrossel / rodízio",
            "LEAST_OPEN": "Menor número de atendimentos pendentes",
            "MANUAL": "Somente distribuição manual",
        },
        audit_logs=audit_logs,
        environment=settings.app_env,
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
        return RedirectResponse("/settings", status_code=303)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)
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
    return RedirectResponse("/settings", status_code=303)


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
        return RedirectResponse("/settings", status_code=303)
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
    return RedirectResponse("/settings", status_code=303)
