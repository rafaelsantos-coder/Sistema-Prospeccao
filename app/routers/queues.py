from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import Branch, ServiceQueue, ServiceQueueMember, User
from app.security import get_current_user, require_roles, validate_csrf
from app.services.ai_sdr import queue_ai_binding
from app.services.audit import write_audit
from app.services.meta_whatsapp import (
    PROVIDER_META,
    PROVIDER_ZAPI,
    bind_queue_meta_account,
    get_meta_account,
    list_meta_accounts,
    queue_meta_account,
)
from app.services.queue_routing import queue_summary
from app.services.sulnet_wa import (
    PROVIDER_SULNET_WA, GatewayConfig, SulnetWAError,
    bind_queue_instance as bind_sulnet_queue_instance,
    list_instances as list_sulnet_wa_instances,
    queue_instance as sulnet_queue_instance,
)
from app.services.zapi_instances import (
    bind_queue_instance,
    get_zapi_instance,
    list_zapi_instances,
    queue_instance,
)
from app.web import flash, render


router = APIRouter(prefix="/settings/queues")
ROUTING_STRATEGIES = {
    "ROUND_ROBIN": "Carrossel / rodízio",
    "LEAST_OPEN": "Menor número de atendimentos pendentes",
    "MANUAL": "Somente distribuição manual",
}
WHATSAPP_PROVIDERS = {
    PROVIDER_ZAPI: "Z-API",
    PROVIDER_META: "API oficial Meta",
    PROVIDER_SULNET_WA: "Motor WhatsApp Sulnet",
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


def _queue_manage_allowed(actor: User, queue: ServiceQueue) -> bool:
    if actor.role in {"ADMIN", "GESTOR"}:
        return True
    if actor.role != "SUPERVISOR":
        return False
    if actor.branch_id:
        return queue.branch_id in {None, actor.branch_id}
    return queue.team == actor.team


def _member_manage_allowed(actor: User, queue: ServiceQueue, target: User) -> bool:
    if not _queue_manage_allowed(actor, queue):
        return False
    if actor.role in {"ADMIN", "GESTOR"}:
        return True
    if actor.branch_id:
        return target.branch_id == actor.branch_id
    return target.team == actor.team


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


def _parse_optional_id(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def _bind_queue_whatsapp(
    db: Session,
    queue: ServiceQueue,
    *,
    provider: str,
    zapi_instance_id: int | None,
    meta_account_id: int | None,
    sulnet_wa_instance_id: int | None = None,
) -> tuple[str, int] | None:
    """Vincula somente um provedor por fila, sem fallback entre canais."""
    clean_provider = (provider or PROVIDER_ZAPI).strip().upper()
    if clean_provider == PROVIDER_SULNET_WA:
        from app.models import SulnetWAInstance
        inst = db.get(SulnetWAInstance, sulnet_wa_instance_id)
        if not inst or not inst.active or not GatewayConfig.from_env().configured:
            return None
        try:
            bind_sulnet_queue_instance(db, queue.id, inst.id)
        except (ValueError, SulnetWAError):
            return None
        return PROVIDER_SULNET_WA, inst.id
    if clean_provider == PROVIDER_META:
        account = get_meta_account(db, meta_account_id)
        if not account or not account.active:
            return None
        try:
            bind_queue_meta_account(db, queue.id, account.id)
        except ValueError:
            return None
        return PROVIDER_META, account.id

    if clean_provider != PROVIDER_ZAPI:
        return None
    instance = get_zapi_instance(db, zapi_instance_id)
    if not instance or not instance.active:
        return None
    bind_queue_instance(db, queue.id, instance.id)
    return PROVIDER_ZAPI, instance.id


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
    whatsapp_provider: str = Form(PROVIDER_ZAPI),
    zapi_instance_id: str = Form(""),
    meta_account_id: str = Form(""),
    sulnet_wa_instance_id: str = Form(""),
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
        return RedirectResponse("/settings/queues", status_code=303)
    if strategy not in ROUTING_STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if db.scalar(select(ServiceQueue.id).where(ServiceQueue.code == clean_code)):
        flash(request, "Já existe uma fila com este código.", "warning")
        return RedirectResponse("/settings/queues", status_code=303)

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

    binding = _bind_queue_whatsapp(
        db,
        queue,
        provider=whatsapp_provider,
        zapi_instance_id=_parse_optional_id(zapi_instance_id),
        meta_account_id=_parse_optional_id(meta_account_id),
        sulnet_wa_instance_id=_parse_optional_id(sulnet_wa_instance_id),
    )
    if not binding:
        db.rollback()
        flash(
            request,
            "Selecione uma conexão WhatsApp ativa e disponível. Número Meta não pode ser compartilhado entre filas.",
            "warning",
        )
        return RedirectResponse("/settings/queues", status_code=303)

    provider_name, connection_id = binding
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
            "whatsapp_provider": provider_name,
            "zapi_instance_id": connection_id if provider_name == PROVIDER_ZAPI else None,
            "meta_account_id": connection_id if provider_name == PROVIDER_META else None,
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
            return RedirectResponse("/settings/queues", status_code=303)
        if not actor.branch_id and queue.team != actor.team:
            return RedirectResponse("/settings/queues", status_code=303)
    users = [item for item in _scoped_users(db, queue) if item.role != "AI_SDR"]
    if actor.role == "SUPERVISOR":
        if actor.branch_id:
            users = [item for item in users if item.branch_id == actor.branch_id]
        else:
            users = [item for item in users if item.team == actor.team]
    member_ids = {item.user_id for item in queue.members if item.active}
    branches = list(
        db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.city))
    )
    selected_zapi = queue_instance(db, queue.id)
    selected_meta = queue_meta_account(db, queue.id)
    selected_sulnet = sulnet_queue_instance(db, queue.id)
    selected_provider = (PROVIDER_SULNET_WA if selected_sulnet else
                         PROVIDER_META if selected_meta else PROVIDER_ZAPI)
    return render(
        request,
        "queue_detail.html",
        user=actor,
        active_nav="settings",
        settings_section="queues",
        page_title=f"Fila · {queue.name}",
        queue=queue,
        users=users,
        member_ids=member_ids,
        branches=branches,
        queue_stats=queue_summary(db, queue),
        ai_binding=queue_ai_binding(db, queue.id),
        zapi_instances=list_zapi_instances(db),
        meta_accounts=list_meta_accounts(db),
        sulnet_wa_instances=[item for item in list_sulnet_wa_instances(db) if item.active],
        selected_sulnet_wa_instance=selected_sulnet,
        selected_zapi_instance=selected_zapi,
        selected_meta_account=selected_meta,
        selected_whatsapp_provider=selected_provider,
        whatsapp_providers=WHATSAPP_PROVIDERS,
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
    whatsapp_provider: str = Form(PROVIDER_ZAPI),
    zapi_instance_id: str = Form(""),
    meta_account_id: str = Form(""),
    sulnet_wa_instance_id: str = Form(""),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    queue = _queue_or_404(db, queue_id)
    if not _queue_manage_allowed(actor, queue):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    strategy = routing_strategy.strip().upper()
    if strategy not in ROUTING_STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    binding = _bind_queue_whatsapp(
        db,
        queue,
        provider=whatsapp_provider,
        zapi_instance_id=_parse_optional_id(zapi_instance_id),
        meta_account_id=_parse_optional_id(meta_account_id),
        sulnet_wa_instance_id=_parse_optional_id(sulnet_wa_instance_id),
    )
    if not binding:
        flash(
            request,
            "Selecione uma conexão WhatsApp ativa e disponível. Número Meta não pode ser compartilhado entre filas.",
            "warning",
        )
        return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)

    provider_name, connection_id = binding
    queue.name = name.strip() or queue.name
    queue.team = team.strip().upper()
    queue.branch_id = int(branch_id) if branch_id and branch_id.isdigit() else None
    queue.routing_strategy = strategy
    queue.is_default_inbound = is_default_inbound
    queue.active = active

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
            "members_managed_separately": True,
            "whatsapp_provider": provider_name,
            "zapi_instance_id": connection_id if provider_name == PROVIDER_ZAPI else None,
            "meta_account_id": connection_id if provider_name == PROVIDER_META else None,
        },
    )
    db.commit()
    flash(request, "Configuração da fila atualizada.")
    return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)


@router.post("/{queue_id}/members/{user_id}/add")
def add_queue_member(
    queue_id: int,
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    queue = _queue_or_404(db, queue_id)
    target = db.get(User, user_id)
    if not target or not target.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not _member_manage_allowed(actor, queue, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    member = db.scalar(
        select(ServiceQueueMember).where(
            ServiceQueueMember.queue_id == queue.id,
            ServiceQueueMember.user_id == target.id,
        )
    )
    if member:
        member.active = True
    else:
        member = ServiceQueueMember(
            queue_id=queue.id,
            user_id=target.id,
            active=True,
        )
        db.add(member)

    write_audit(
        db,
        user=actor,
        action="SERVICE_QUEUE_MEMBER_ADDED",
        entity_type="service_queue",
        entity_id=queue.id,
        payload={"user_id": target.id, "user_name": target.name},
    )
    db.commit()
    flash(request, f"{target.name} foi adicionado à fila {queue.name}.")
    return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)


@router.post("/{queue_id}/members/{user_id}/remove")
def remove_queue_member(
    queue_id: int,
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)

    queue = _queue_or_404(db, queue_id)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not _member_manage_allowed(actor, queue, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    member = db.scalar(
        select(ServiceQueueMember).where(
            ServiceQueueMember.queue_id == queue.id,
            ServiceQueueMember.user_id == target.id,
        )
    )
    if member:
        member.active = False

    write_audit(
        db,
        user=actor,
        action="SERVICE_QUEUE_MEMBER_REMOVED",
        entity_type="service_queue",
        entity_id=queue.id,
        payload={"user_id": target.id, "user_name": target.name},
    )
    db.commit()
    flash(
        request,
        f"{target.name} foi removido da fila. Conversas já atribuídas permanecem com o atendente até serem transferidas.",
    )
    return RedirectResponse(f"/settings/queues/{queue.id}", status_code=303)
