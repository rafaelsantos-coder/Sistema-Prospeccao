from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import AuditLog, Branch, ServiceQueue, ServiceQueueMember, Target, User
from app.security import get_current_user, hash_password, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.legacy_adapter import legacy_modules_available, rate_status
from app.services.queue_routing import queue_summary
from app.services.zapi import ZApiError, ZApiService
from app.web import render


router = APIRouter(prefix="/settings")


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return RedirectResponse("/dashboard", status_code=303)
    branches = list(db.scalars(select(Branch).order_by(Branch.city)))
    users = list(db.scalars(select(User).order_by(User.name)))
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
    zapi_status = {
        "configured": settings.integrations["zapi"],
        "connected": False,
        "smartphone_connected": False,
        "error": None,
        "webhook_ready": bool(settings.zapi_webhook_url),
    }
    if settings.integrations["zapi"]:
        try:
            zapi_status.update(ZApiService().status())
        except ZApiError as exc:
            zapi_status["error"] = str(exc)
    return render(
        request,
        "settings.html",
        user=user,
        active_nav="settings",
        page_title="Configurações",
        integrations=settings.integrations,
        zapi_status=zapi_status,
        legacy_available=legacy_modules_available(),
        rate_status=rate_status(),
        branches=branches,
        users=users,
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
        from app.web import flash
        flash(request, "Ja existe um usuario com este e-mail.", "warning")
        return RedirectResponse("/settings", status_code=303)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        from app.web import flash
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
    from app.web import flash
    flash(request, f"Usuario {created.name} criado. A troca de senha sera exigida no primeiro acesso.")
    return RedirectResponse("/settings", status_code=303)


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
        from app.web import flash
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
