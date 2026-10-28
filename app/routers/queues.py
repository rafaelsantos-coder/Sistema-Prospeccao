from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Branch, ServiceQueue, ServiceQueueMember, User
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.queue_routing import queue_summary
from app.web import flash, render


router = APIRouter(prefix="/settings/queues")
ROUTING_STRATEGIES = {
    "ROUND_ROBIN": "Carrossel / rodízio",
    "LEAST_OPEN": "Menor número de atendimentos pendentes",
    "MANUAL": "Somente distribuição manual",
}


def _actor(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _clean_code(value: str) -> str:
    clean = re.sub(r"[^A-Z0-9_-]+", "-", value.strip().upper())
    return clean.strip("-_")[:40]


def _queue_or_404(db: Session, queue_id: int) -> ServiceQueue:
    queue = db.scalar(
        select(ServiceQueue)
        .options(
            selectinload(ServiceQueue.branch),
            selectinload(ServiceQueue.members).selectinload(ServiceQueueMember.user),
        )
        .where(ServiceQueue.id == queue_id)
    )
    if not queue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return queue


def _scoped_users(db: Session, queue: ServiceQueue | None = None) -> list[User]:
    stmt = select(User).where(User.active.is_(True)).order_by(User.name)
    users = list(db.scalars(stmt))
    if queue and queue.branch_id:
        same_branch = [item for item in users if item.branch_id == queue.branch_id]
        if same_branch:
            users = same_branch
    return users


def _unset_other_defaults(db: Session, queue: ServiceQueue) -> None:
    if not queue.is_default_inbound:
        return
    stmt = select(ServiceQueue).where(
        ServiceQueue.id != queue.id,
        ServiceQueue.team == queue.team,
        ServiceQueue.is_default_inbound.is_(True),
    )
    if queue.branch_id:
        stmt = stmt.where(ServiceQueue.branch_id == queue.branch_id)
    else:
        stmt = stmt.where(ServiceQueue.branch_id.is_(None))
    for item in db.scalars(stmt):
        item.is_default_inbound = False


@router.post("")
def create_queue(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    code: str = Form(...),
    team: str = Form("B2B"),
    branch_id: str = Form(""),
    routing_strategy: str = Form("ROUND_ROBIN"),
    is_default_inbound: bool = Form(False),
    member_user_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")

    clean_name = name.strip()
    clean_code = _clean_code(code)
    strategy = routing_strategy.strip().upper()
    if not clean_name or not clean_code:
        flash(request, "Informe nome e código da fila.", "warning")
        return RedirectResponse("/settings", status_code=303)
    if strategy not in ROUTING_STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if db.scalar(select(ServiceQueue.id).where(ServiceQueue.code == clean_code)):
        flash(request, "Já existe uma fila com este código.", "warning")
        return RedirectResponse("/settings", status_code=303)

    branch_value = int(branch_id) if branch_id and branch_id.isdigit() else None
    queue = ServiceQueue(
        name=clean_name,
        code=clean_code,
        team=team.strip().upper(),
        branch_id=branch_value,
        routing_strategy=strategy,
        is_default_inbound=is_default_inbound,
        active=True,
        created_by_id=actor.id,
    )
    db.add(queue)
    db.flush()
    for user_id in sorted(set(member_user_ids)):
        user = db.get(User, user_id)
        if user and user.active:
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id))
    _unset_other_defaults(db, queue)
    write_audit(
        db,
        user=actor,
        action="SERVICE_QUEUE_CREATED",
        entity_type="service_queue",
        entity_id=queue.id,
        payload={
            "code": queue.code,
            "team": queue.team,
            "routing_strategy": queue.routing_strategy,
            "members": sorted(set(member_user_ids)),
        },
    )
    db.commit()
    flash(request, f"Fila {queue.name} criada.")
    return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)


@router.get("/{queue_id}")
def queue_detail(
    queue_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    queue = _queue_or_404(db, queue_id)
    if actor.role == "SUPERVISOR":
        if actor.branch_id and queue.branch_id not in {None, actor.branch_id}:
            return RedirectResponse("/settings", status_code=303)
        if not actor.branch_id and queue.team != actor.team:
            return RedirectResponse("/settings", status_code=303)
    users = _scoped_users(db, queue)
    member_ids = {item.user_id for item in queue.members if item.active}
    branches = list(
        db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city))
    )
    return render(
        request,
        "queue_detail.html",
        user=actor,
        active_nav="settings",
        page_title=f"Fila · {queue.name}",
        queue=queue,
        users=users,
        member_ids=member_ids,
        branches=branches,
        queue_stats=queue_summary(db, queue),
        routing_strategies=ROUTING_STRATEGIES,
    )


@router.post("/{queue_id}")
def update_queue(
    queue_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    team: str = Form("B2B"),
    branch_id: str = Form(""),
    routing_strategy: str = Form("ROUND_ROBIN"),
    is_default_inbound: bool = Form(False),
    active: bool = Form(False),
    member_user_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    queue = _queue_or_404(db, queue_id)
    strategy = routing_strategy.strip().upper()
    if strategy not in ROUTING_STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    queue.name = name.strip() or queue.name
    queue.team = team.strip().upper()
    queue.branch_id = int(branch_id) if branch_id and branch_id.isdigit() else None
    queue.routing_strategy = strategy
    queue.is_default_inbound = is_default_inbound
    queue.active = active

    selected = set(member_user_ids)
    existing = {item.user_id: item for item in queue.members}
    for user_id, member in existing.items():
        member.active = user_id in selected
    for user_id in sorted(selected - set(existing)):
        user = db.get(User, user_id)
        if user and user.active:
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id, active=True))

    _unset_other_defaults(db, queue)
    write_audit(
        db,
        user=actor,
        action="SERVICE_QUEUE_UPDATED",
        entity_type="service_queue",
        entity_id=queue.id,
        payload={
            "team": queue.team,
            "routing_strategy": queue.routing_strategy,
            "active": queue.active,
            "default_inbound": queue.is_default_inbound,
            "members": sorted(selected),
        },
    )
    db.commit()
    flash(request, "Configuração da fila atualizada.")
    return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)
