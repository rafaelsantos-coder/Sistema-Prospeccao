from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import JSONResponse, RedirectResponse

from app.database import get_db
from app.models import (
    Activity,
    AIAgentProfile,
    Contact,
    Conversation,
    ConversationAssignment,
    ConversationQueueLink,
    ConversationCommercialInsight,
    ConversationCommercialTag,
    Lead,
    Message,
    ServiceQueue,
    User,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.ai_sdr import enqueue_ai_job, latest_run_for_conversation
from app.services.audit import write_audit
from app.services.global_blacklist import (
    BlacklistPhoneError,
    block_phone,
    is_globally_blocked,
)
from app.services.commercial_intelligence import (
    TAG_LABELS,
    TEMPERATURE_LABELS,
    STAGE_LABELS,
    OWNER_LABELS,
    tag_map,
    insight_map,
    update_manual_tag,
)
from app.services.conversation_engine import (
    CLOSED_CONVERSATION_STATUSES,
    OPEN_CONVERSATION_STATUSES,
    apply_conversation_scope,
    location_urls_for,
    outbound_without_reply_clause,
    conversation_visible_to_user,
    ensure_contact,
    ensure_open_conversation,
    mark_conversation_read,
    register_message,
)
from app.services.crm_manual_registration import (
    CRMManualError,
    register_conversation,
    registration_status,
    seller_for_user,
)
from app.services.media_processing import MediaProcessingError, normalize_audio
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.metrics import apply_user_scope
from app.services.pii import redact_pii_text
from app.services.upload_security import UploadValidationError, validate_upload
from app.services.queue_routing import (
    MANUAL_TRANSFER_ORIGIN,
    ai_queue_ids,
    ai_transfer_queue,
    assign_queue_user,
    available_queues_for_user,
    bind_conversation_queue,
    get_queue,
    queue_for_conversation,
    queue_member_user_ids,
    usable_ai_binding,
)
from app.services.whatsapp_presence import is_user_whatsapp_online, online_user_ids
from app.services.zapi import ZApiError
from app.services.zapi_instances import service_for_conversation
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter()


def _user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        flash(
            request,
            "Entre no WhatsApp para acessar o canal de conversas e receber atendimentos.",
            "warning",
        )
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _conversation_stmt(user: User):
    return apply_conversation_scope(
        select(Conversation).options(
            selectinload(Conversation.contact),
            selectinload(Conversation.lead),
            selectinload(Conversation.assigned_user),
            selectinload(Conversation.branch),
        ),
        user,
        inbox=True,
    )


def _conversation_unavailable(request: Request) -> RedirectResponse:
    """Conversa que saiu da lista do usuário (reatribuída, IA assumiu): volta à lista, sem tela de erro."""
    flash(
        request,
        "Esta conversa não está mais com você (foi reatribuída ou a IA assumiu). "
        "Se o cliente pedir, ela volta para a sua lista.",
        "warning",
    )
    return RedirectResponse("/conversations", status_code=303)


def _get_conversation(db: Session, user: User, conversation_id: int) -> Conversation | None:
    return db.scalar(_conversation_stmt(user).where(Conversation.id == conversation_id))


def _contact_history_messages(db: Session, conversation: Conversation) -> list[Message]:
    return list(
        db.scalars(
            select(Message)
            .where(Message.contact_id == conversation.contact_id)
            .options(selectinload(Message.sender_user))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )


def _message_visible_through_contact(
    db: Session,
    user: User,
    message: Message,
) -> bool:
    if not message.contact_id:
        return False
    visible_conversation = db.scalar(
        _conversation_stmt(user)
        .where(Conversation.contact_id == message.contact_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )
    return visible_conversation is not None


def _eligible_users(db: Session, user: User, conversation: Conversation | None = None) -> list[User]:
    """Vendedores para quem a conversa pode ser transferida, online ou não.

    Antes só apareciam os online: com a equipe toda offline o painel sumia e a
    conversa ficava parada na fila (conversa 1407, 09/11). Vendedor offline
    recebe a conversa e vê quando entrar no WhatsApp.
    """
    stmt = select(User).where(
        User.active.is_(True),
        User.role == "CONSULTOR",
    ).order_by(User.name)
    users = list(db.scalars(stmt))
    ai_user_ids = {item for item in db.scalars(select(AIAgentProfile.user_id)) if item}
    users = [item for item in users if item.id not in ai_user_ids]

    if conversation:
        service_queue = queue_for_conversation(db, conversation.id)
        if service_queue:
            member_ids = set(queue_member_user_ids(db, service_queue.id))
            users = [item for item in users if item.id in member_ids]
        elif conversation.team:
            users = [item for item in users if item.team == conversation.team]

    if user.role == "SUPERVISOR":
        if user.branch_id:
            users = [item for item in users if item.branch_id == user.branch_id]
        else:
            users = [item for item in users if item.team == user.team]
    elif user.role == "CONSULTOR":
        users = [item for item in users if item.team == user.team]
        if user.branch_id:
            users = [item for item in users if item.branch_id == user.branch_id]

    if conversation and conversation.branch_id:
        same_branch = [item for item in users if item.branch_id == conversation.branch_id]
        if same_branch:
            users = same_branch

    if conversation and conversation.assigned_user_id:
        users = [item for item in users if item.id != conversation.assigned_user_id]
    return users


def _transfer_queues(
    db: Session,
    user: User,
    current_queue: ServiceQueue | None,
) -> list[dict]:
    """Filas de destino da transferência (sem a fila atual), marcando as que têm IA.

    Vendedor vê as filas de que participa e também as filas com IA, para poder
    devolver a conversa à IA.
    """
    ai_ids = ai_queue_ids(db)
    queues = {item.id: item for item in available_queues_for_user(db, user)}
    if ai_ids:
        for item in db.scalars(select(ServiceQueue).where(ServiceQueue.id.in_(ai_ids))):
            queues.setdefault(item.id, item)
    if current_queue:
        queues.pop(current_queue.id, None)
    return [
        {"id": item.id, "name": item.name, "ai": item.id in ai_ids}
        for item in sorted(queues.values(), key=lambda queue: (queue.name or "", queue.id))
        if item.active
    ]


def _pending_inbound_message(db: Session, conversation: Conversation) -> Message | None:
    """Última mensagem de texto do cliente que ainda não teve resposta nossa."""
    latest = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction.in_(("INBOUND", "OUTBOUND")),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    if not latest or latest.direction != "INBOUND" or latest.message_type != "TEXT":
        return None
    return latest


_RETRYABLE_AI_JOB_STATUSES = {"FALHOU", "IGNORADO", "CANCELADO"}


def _move_conversation_to_queue(
    db: Session,
    *,
    actor: User,
    conversation: Conversation,
    queue: ServiceQueue,
    reason: str,
) -> dict:
    """Troca a fila da conversa e entrega para a IA da fila ou para um vendedor online."""
    previous_queue = queue_for_conversation(db, conversation.id)
    previous_user_id = conversation.assigned_user_id
    bind_conversation_queue(db, conversation.id, queue.id, origin=MANUAL_TRANSFER_ORIGIN)
    conversation.team = queue.team
    conversation.assigned_user_id = None
    if conversation.status not in CLOSED_CONVERSATION_STATUSES:
        conversation.status = "EM_FILA"

    result = {"ai_job": None, "assigned": None, "pending_message": None}
    binding = usable_ai_binding(db, queue.id)
    if binding:
        message = _pending_inbound_message(db, conversation)
        result["pending_message"] = message
        if message:
            # Transferência manual para a IA: responde já, sem esperar os 10 min
            # da fila comercial.
            job = enqueue_ai_job(
                db,
                conversation=conversation,
                message=message,
                takeover=True,
            )
            if job and job.status in _RETRYABLE_AI_JOB_STATUSES:
                # Job dessa mensagem já tinha falhado (ex.: OpenAI sem crédito em
                # 09/11): a transferência manual dá uma nova chance.
                job.status = "PENDENTE"
                job.attempts = 0
                job.locked_at = None
                job.available_at = utcnow()
                job.agent_id = binding.agent_id
                job.queue_id = queue.id
                job.error = f"[transferência manual] {job.error or ''}"[:2000]
            if job and job.status in {"PENDENTE", "PROCESSANDO"}:
                result["ai_job"] = job
    else:
        target = assign_queue_user(db, queue)
        if target:
            conversation.assigned_user_id = target.id
            conversation.status = "ATRIBUIDO"
            if conversation.lead:
                conversation.lead.assigned_user_id = target.id
            db.add(
                ConversationAssignment(
                    conversation_id=conversation.id,
                    user_id=target.id,
                    assigned_by_id=actor.id,
                    event="TRANSFERRED" if previous_user_id else "ASSIGNED",
                )
            )
            result["assigned"] = target

    write_audit(
        db,
        user=actor,
        action="CONVERSATION_QUEUE_TRANSFERRED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "reason": reason,
            "from_queue_id": previous_queue.id if previous_queue else None,
            "to_queue_id": queue.id,
            "from_user_id": previous_user_id,
            "to_user_id": result["assigned"].id if result["assigned"] else None,
            "ai_job_id": result["ai_job"].id if result["ai_job"] else None,
        },
    )
    return result


def _inbox_query(
    db: Session,
    user: User,
    *,
    q: str,
    view: str,
    assignee_id: int | None = None,
    queue_id: int | None = None,
    tag_code: str = "",
    temperature: str = "",
) -> list[Conversation]:
    stmt = _conversation_stmt(user).join(Contact, Conversation.contact_id == Contact.id)
    if tag_code:
        if tag_code not in TAG_LABELS:
            raise HTTPException(status_code=400, detail="Etiqueta inválida")
        stmt = stmt.join(
            ConversationCommercialTag,
            ConversationCommercialTag.conversation_id == Conversation.id,
        ).where(
            ConversationCommercialTag.code == tag_code,
            ConversationCommercialTag.suppressed.is_(False),
        )
    if temperature:
        if temperature not in {"QUENTE", "MORNO", "FRIO", "INDEFINIDO"}:
            raise HTTPException(status_code=400, detail="Temperatura inválida")
        if temperature == "INDEFINIDO":
            stmt = stmt.outerjoin(
                ConversationCommercialInsight,
                ConversationCommercialInsight.conversation_id == Conversation.id,
            ).where(
                or_(ConversationCommercialInsight.id.is_(None),
                    ConversationCommercialInsight.temperature == "INDEFINIDO")
            )
        else:
            stmt = stmt.join(
                ConversationCommercialInsight,
                ConversationCommercialInsight.conversation_id == Conversation.id,
            ).where(ConversationCommercialInsight.temperature == temperature)
    if assignee_id:
        stmt = stmt.where(Conversation.assigned_user_id == assignee_id)
    if queue_id is not None:
        stmt = stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id == queue_id)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.outerjoin(Lead, Conversation.lead_id == Lead.id).where(
            or_(
                Contact.phone.ilike(term),
                Contact.name.ilike(term),
                Contact.business_name.ilike(term),
                Lead.business_name.ilike(term),
                Lead.contact_name.ilike(term),
            )
        )
    if view == "unread":
        stmt = stmt.where(Conversation.unread_count > 0)
    elif view == "pending":
        stmt = stmt.where(
            Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            or_(
                Conversation.unread_count > 0,
                Conversation.assigned_user_id.is_(None),
                Conversation.status.in_(("AGUARDANDO_INTERNO", "FOLLOW_UP")),
            ),
        )
    elif view == "closed":
        stmt = stmt.where(Conversation.status.in_(CLOSED_CONVERSATION_STATUSES))
    elif view == "queue":
        stmt = stmt.where(
            Conversation.assigned_user_id.is_(None),
            Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
        )
    elif view == "mine" and user.role in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        stmt = stmt.where(Conversation.assigned_user_id == user.id)
    else:
        stmt = stmt.where(Conversation.status.in_(OPEN_CONVERSATION_STATUSES))
    stmt = stmt.order_by(
        Conversation.last_message_at.desc().nullslast(),
        Conversation.updated_at.desc(),
    ).limit(250)
    return list(db.scalars(stmt))


def _needs_attention(
    conversation_status: str, unread_count: int | None, assigned_user_id: int | None
) -> bool:
    """Pendência operacional: mensagem não lida, sem dono ou tarefa interna/follow-up."""
    return conversation_status in OPEN_CONVERSATION_STATUSES and (
        bool(unread_count)
        or assigned_user_id is None
        or conversation_status in {"AGUARDANDO_INTERNO", "FOLLOW_UP"}
    )


def _inbox_counts(
    db: Session,
    user: User,
    *,
    queue_id: int | None = None,
    assignee_id: int | None = None,
    tag_code: str = "",
    temperature: str = "",
) -> dict[str, int]:
    """Contadores com os mesmos filtros da caixa (sem o limite de 250 itens)."""
    stmt = apply_conversation_scope(select(Conversation), user, inbox=True)
    if tag_code:
        if tag_code not in TAG_LABELS:
            raise HTTPException(status_code=400, detail="Etiqueta inválida")
        stmt = stmt.join(ConversationCommercialTag,
            ConversationCommercialTag.conversation_id == Conversation.id).where(
            ConversationCommercialTag.code == tag_code,
            ConversationCommercialTag.suppressed.is_(False))
    if temperature:
        if temperature not in {"QUENTE", "MORNO", "FRIO", "INDEFINIDO"}:
            raise HTTPException(status_code=400, detail="Temperatura inválida")
        if temperature == "INDEFINIDO":
            stmt = stmt.outerjoin(ConversationCommercialInsight,
                ConversationCommercialInsight.conversation_id == Conversation.id).where(
                or_(ConversationCommercialInsight.id.is_(None),
                    ConversationCommercialInsight.temperature == "INDEFINIDO"))
        else:
            stmt = stmt.join(ConversationCommercialInsight,
                ConversationCommercialInsight.conversation_id == Conversation.id).where(
                ConversationCommercialInsight.temperature == temperature)
    if queue_id is not None:
        stmt = stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id == queue_id)
    if assignee_id is not None:
        stmt = stmt.where(Conversation.assigned_user_id == assignee_id)
    scoped = list(db.scalars(stmt))
    open_items = [item for item in scoped if item.status in OPEN_CONVERSATION_STATUSES]
    return {
        "active": len(open_items),
        "unread": sum(1 for item in scoped if item.unread_count > 0),
        "closed": sum(1 for item in scoped if item.status in CLOSED_CONVERSATION_STATUSES),
        "queue": sum(1 for item in open_items if item.assigned_user_id is None),
        "pending": sum(
            1 for item in open_items
            if _needs_attention(item.status, item.unread_count, item.assigned_user_id)
        ),
        "waiting_client": sum(1 for item in open_items if item.status == "AGUARDANDO_CLIENTE"),
        "waiting_internal": sum(1 for item in open_items if item.status == "AGUARDANDO_INTERNO"),
        "follow_up": sum(1 for item in open_items if item.status == "FOLLOW_UP"),
    }


def _seller_panel(db: Session, user: User, *, queue_id: int | None = None) -> list[dict]:
    """Vendedores visíveis para o usuário, com presença online e carga de conversas abertas."""
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return []
    stmt = select(User).where(User.active.is_(True), User.role == "CONSULTOR").order_by(User.name)
    sellers = list(db.scalars(stmt))
    if user.role == "SUPERVISOR":
        if user.branch_id:
            sellers = [item for item in sellers if item.branch_id == user.branch_id]
        else:
            sellers = [item for item in sellers if item.team == user.team]
    online_ids = set(online_user_ids(db))
    # Agente de IA não faz login: está online enquanto o perfil estiver ativo.
    ai_user_ids = set(
        db.scalars(
            select(AIAgentProfile.user_id).where(
                AIAgentProfile.active.is_(True),
                AIAgentProfile.user_id.is_not(None),
            )
        )
    )
    online_ids |= ai_user_ids
    open_counts: dict[int, int] = {}
    stmt = select(Conversation.assigned_user_id).where(
        Conversation.assigned_user_id.is_not(None),
        Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
        ~outbound_without_reply_clause(),
    )
    if queue_id is not None:
        stmt = stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id == queue_id)
    for assigned_id in db.scalars(stmt):
        open_counts[assigned_id] = open_counts.get(assigned_id, 0) + 1
    return [
        {
            "id": item.id,
            "name": item.name,
            "online": item.id in online_ids,
            "open": open_counts.get(item.id, 0),
        }
        for item in sellers
    ]


def _queue_panel(db: Session, user: User) -> list[dict]:
    """Filas ativas com quantas conversas abertas cada uma tem agora."""
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return []
    queues = list(db.scalars(select(ServiceQueue).where(ServiceQueue.active.is_(True)).order_by(ServiceQueue.name)))
    if user.role == "SUPERVISOR":
        if user.branch_id:
            queues = [item for item in queues if item.branch_id == user.branch_id]
        else:
            queues = [item for item in queues if item.team == user.team]
    open_counts: dict[int, int] = {}
    pending_counts: dict[int, int] = {}
    for queue_id, conversation_status, unread_count, assigned_user_id in db.execute(
        select(
            ConversationQueueLink.queue_id,
            Conversation.status,
            Conversation.unread_count,
            Conversation.assigned_user_id,
        )
        .join(Conversation, Conversation.id == ConversationQueueLink.conversation_id)
        .where(Conversation.status.in_(OPEN_CONVERSATION_STATUSES), ~outbound_without_reply_clause())
    ):
        open_counts[queue_id] = open_counts.get(queue_id, 0) + 1
        if _needs_attention(conversation_status, unread_count, assigned_user_id):
            pending_counts[queue_id] = pending_counts.get(queue_id, 0) + 1
    return [
        {
            "id": item.id,
            "name": item.name,
            "open": open_counts.get(item.id, 0),
            "pending": pending_counts.get(item.id, 0),
        }
        for item in queues
    ]


def _validate_queue_filter(user: User, queue_id: int | None, queues: list[dict]) -> None:
    """Não permitir consultar filas fora do escopo gerencial do usuário."""
    if queue_id is None:
        return
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"} or queue_id not in {
        item["id"] for item in queues
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fila indisponível para o seu perfil.",
        )


def _render_inbox(
    request: Request,
    db: Session,
    user: User,
    *,
    conversation_id: int | None = None,
    q: str = "",
    view: str = "active",
    assignee_id: int | None = None,
    queue_id: int | None = None,
    tag_code: str = "",
    temperature: str = "",
):
    queues = _queue_panel(db, user)
    _validate_queue_filter(user, queue_id, queues)
    focused_queue = next((item for item in queues if item["id"] == queue_id), None)
    conversations = _inbox_query(
        db, user, q=q, view=view, assignee_id=assignee_id, queue_id=queue_id,
        tag_code=tag_code, temperature=temperature,
    )
    selected = None
    if conversation_id is not None:
        selected = _get_conversation(db, user, conversation_id)
        if not selected:
            return _conversation_unavailable(request)
    elif conversations:
        selected = conversations[0]

    messages: list[Message] = []
    users: list[User] = []
    selected_queue = None
    latest_ai_run = None
    if selected:
        selected_queue = queue_for_conversation(db, selected.id)
        latest_ai_run = latest_run_for_conversation(db, selected.id)
        if selected.unread_count:
            mark_conversation_read(db, selected)
            db.commit()
        messages = _contact_history_messages(db, selected)
        users = _eligible_users(db, user, selected)
        transfer_online_ids = set(online_user_ids(db))
        users.sort(key=lambda item: (item.id not in transfer_online_ids, item.name or ""))
        ai_queue = ai_transfer_queue(db, selected.id)
        transfer_queues = _transfer_queues(db, user, selected_queue)
    else:
        transfer_online_ids = set()
        ai_queue = None
        transfer_queues = []

    closer_escalated = False
    if selected and user.role in {"ADMIN", "GESTOR"}:
        from app.models import AIClosingState

        closer_escalated = bool(
            db.scalar(
                select(AIClosingState.id).where(
                    AIClosingState.conversation_id == selected.id,
                    AIClosingState.step == "ESCALATED",
                )
            )
        )

    counts = _inbox_counts(
        db, user, queue_id=queue_id, assignee_id=assignee_id,
        tag_code=tag_code, temperature=temperature,
    )
    visible_ids = [item.id for item in conversations]
    if selected and selected.id not in visible_ids:
        visible_ids.append(selected.id)
    commercial_tags = tag_map(db, visible_ids)
    commercial_insights = insight_map(db, visible_ids)
    return render(
        request,
        "conversations/inbox.html",
        closer_escalated=closer_escalated,
        user=user,
        active_nav="conversations",
        page_title="Conversas",
        conversations=conversations,
        conversation=selected,
        messages=messages,
        location_urls=location_urls_for(db, messages),
        users=users,
        transfer_online_ids=transfer_online_ids,
        ai_transfer_queue=ai_queue,
        transfer_queues=transfer_queues,
        selected_queue=selected_queue,
        latest_ai_run=latest_ai_run,
        counts=counts,
        query=q,
        view=view,
        assignee_id=assignee_id,
        queue_id=queue_id,
        sellers=_seller_panel(db, user, queue_id=queue_id),
        queues=queues,
        focused_queue=focused_queue,
        tag_code=tag_code,
        temperature=temperature,
        tag_catalog=TAG_LABELS,
        temperature_catalog=TEMPERATURE_LABELS,
        stage_catalog=STAGE_LABELS,
        owner_catalog=OWNER_LABELS,
        commercial_tags=commercial_tags,
        commercial_insights=commercial_insights,
        selected_tags=commercial_tags.get(selected.id, []) if selected else [],
        selected_insight=commercial_insights.get(selected.id) if selected else None,
        globally_blacklisted=is_globally_blocked(db, selected.contact.phone) if selected else False,
        crm_manual_seller=seller_for_user(selected.assigned_user) if selected else None,
        crm_manual_status=registration_status(db, selected.contact_id) if selected else None,
        open_statuses=OPEN_CONVERSATION_STATUSES,
        closed_statuses=CLOSED_CONVERSATION_STATUSES,
    )


@router.get("/conversations")
def inbox(
    request: Request,
    q: str = "",
    view: str = "active",
    assignee: int | None = None,
    queue: int | None = None,
    tag: str = "",
    temperature: str = "",
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    return _render_inbox(
        request, db, user, q=q, view=view, assignee_id=assignee, queue_id=queue,
        tag_code=tag, temperature=temperature,
    )


@router.get("/conversations/{conversation_id}")
def conversation_detail(
    conversation_id: int,
    request: Request,
    q: str = "",
    view: str = "active",
    assignee: int | None = None,
    queue: int | None = None,
    tag: str = "",
    temperature: str = "",
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    return _render_inbox(
        request,
        db,
        user,
        conversation_id=conversation_id,
        assignee_id=assignee,
        queue_id=queue,
        q=q,
        view=view,
        tag_code=tag,
        temperature=temperature,
    )


@router.post("/leads/{lead_id}/conversation")
def start_lead_conversation(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    lead = db.scalar(apply_user_scope(select(Lead).where(Lead.id == lead_id), user))
    if not lead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not lead.phone:
        flash(request, "O lead precisa ter telefone para abrir uma conversa.", "warning")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    try:
        contact = ensure_contact(db, lead.phone, lead=lead)
    except ValueError:
        flash(request, "O telefone do lead nao e valido para WhatsApp.", "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    if user.role == "CONSULTOR" and is_user_whatsapp_online(db, user.id):
        lead.assigned_user_id = user.id
        db.flush()
    conversation = ensure_open_conversation(db, contact, lead=lead, source=lead.source)
    if (
        not conversation.assigned_user_id
        and user.role == "CONSULTOR"
        and is_user_whatsapp_online(db, user.id)
    ):
        conversation.assigned_user_id = user.id
        conversation.status = "ATRIBUIDO"
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=user.id,
                assigned_by_id=user.id,
                event="CLAIMED",
            )
        )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/send")
def send_message(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        return _conversation_unavailable(request)
    if is_globally_blocked(db, conversation.contact.phone) or (conversation.lead and (conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR")):
        flash(request, "Este contato esta bloqueado para novas mensagens.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    clean_message = message.strip()
    if not clean_message:
        flash(request, "Digite uma mensagem antes de enviar.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    try:
        result = service_for_conversation(db, conversation.id).send_text(
            phone=conversation.contact.phone,
            message=clean_message,
        )
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)

    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    sent = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=clean_message,
        sender_user_id=user.id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    if conversation.lead:
        conversation.lead.last_contact_at = utcnow()
        db.add(
            Activity(
                lead_id=conversation.lead.id,
                user_id=user.id,
                kind="WHATSAPP",
                status="CONCLUIDA",
                channel="WHATSAPP",
                outcome=f"CHAT_ZAPI:{provider_id or zaap_id or sent.id}"[:120],
                description=(
                    "Mensagem enviada pelo Inbox: "
                    + redact_pii_text(clean_message)[:500]
                ),
                completed_at=utcnow(),
            )
        )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_MESSAGE_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"message_id": sent.id, "provider_message_id": provider_id},
    )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


def _media_kind(filename: str, mime_type: str) -> str:
    clean_mime = (mime_type or "").split(";")[0].strip().lower()
    extension = Path(filename or "").suffix.lower()
    if clean_mime.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "IMAGE"
    if clean_mime.startswith("video/") or extension in {".mp4", ".mov", ".m4v", ".3gp"}:
        return "VIDEO"
    if clean_mime.startswith("audio/") or extension in {".mp3", ".ogg", ".opus", ".wav", ".m4a", ".aac", ".amr"}:
        return "AUDIO"
    return "DOCUMENT"


@router.post("/conversations/{conversation_id}/media")
async def send_media(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    media: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        return _conversation_unavailable(request)
    if is_globally_blocked(db, conversation.contact.phone) or (
        conversation.lead and (
            conversation.lead.do_not_contact
            or conversation.lead.stage == "NAO_CONTATAR"
        )
    ):
        return JSONResponse(
            {"ok": False, "error": "Este contato esta bloqueado para novas mensagens."},
            status_code=403,
        )

    original_name = Path(media.filename or "arquivo").name
    raw = await media.read()
    mime_type = (media.content_type or "application/octet-stream").split(";")[0].strip()
    try:
        validate_upload(
            filename=original_name,
            mime_type=mime_type,
            data=raw,
        )
    except UploadValidationError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    kind = _media_kind(original_name, mime_type)

    if kind == "AUDIO":
        try:
            raw, original_name, mime_type = normalize_audio(
                raw,
                filename=original_name,
                mime_type=mime_type,
            )
        except MediaProcessingError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    storage = MediaStorage()
    try:
        storage_uri, safe_name = storage.upload_bytes(
            data=raw,
            conversation_id=conversation.id,
            filename=original_name,
            mime_type=mime_type,
            direction="OUTBOUND",
        )
        delivery_url = storage.presigned_download_url(
            storage_uri,
            filename=safe_name,
            expires_seconds=3600,
        )
    except MediaStorageError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    clean_caption = caption.strip()
    service = service_for_conversation(db, conversation.id)
    try:
        if kind == "IMAGE":
            result = service.send_image(
                phone=conversation.contact.phone,
                image_url=delivery_url,
                caption=clean_caption,
            )
            body = clean_caption or "[Imagem]"
        elif kind == "VIDEO":
            result = service.send_video(
                phone=conversation.contact.phone,
                video_url=delivery_url,
                caption=clean_caption,
            )
            body = clean_caption or "[Video]"
        elif kind == "AUDIO":
            result = service.send_audio(
                phone=conversation.contact.phone,
                audio_url=delivery_url,
            )
            body = "[Audio]"
        else:
            document_extension = Path(safe_name).suffix.lstrip(".") or "bin"
            result = service.send_document(
                phone=conversation.contact.phone,
                document_url=delivery_url,
                extension=document_extension,
                file_name=safe_name,
                caption=clean_caption,
            )
            body = safe_name
    except ZApiError as exc:
        failed = register_message(
            db,
            conversation,
            direction="OUTBOUND",
            message_type=kind,
            body=clean_caption or safe_name,
            sender_user_id=user.id,
            media_url=storage_uri,
            mime_type=mime_type,
            status="FAILED",
        )
        write_audit(
            db,
            user=user,
            action="CONVERSATION_MEDIA_FAILED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "message_id": failed.id,
                "type": kind,
                "file_name": safe_name,
                "size_bytes": len(raw),
            },
        )
        db.commit()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    sent = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=kind,
        body=body,
        sender_user_id=user.id,
        media_url=storage_uri,
        mime_type=mime_type,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    if conversation.lead:
        conversation.lead.last_contact_at = utcnow()
        db.add(
            Activity(
                lead_id=conversation.lead.id,
                user_id=user.id,
                kind="WHATSAPP",
                status="CONCLUIDA",
                channel="WHATSAPP",
                outcome=f"CHAT_MEDIA_ZAPI:{provider_id or zaap_id or sent.id}"[:120],
                description=f"{kind} enviado pelo Inbox: {safe_name}",
                completed_at=utcnow(),
            )
        )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_MEDIA_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "message_id": sent.id,
            "provider_message_id": provider_id,
            "type": kind,
            "file_name": safe_name,
            "size_bytes": len(raw),
        },
    )
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "message_id": sent.id,
            "provider_message_id": provider_id,
            "type": kind,
        }
    )


@router.get("/conversations/messages/{message_id}/media")
def conversation_media(
    message_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        return RedirectResponse("/dashboard", status_code=303)
    message = db.get(Message, message_id)
    if not message or not message.media_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not _message_visible_through_contact(db, user, message):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        url = MediaStorage().presigned_download_url(
            message.media_url,
            filename=message.body if message.message_type == "DOCUMENT" else None,
            expires_seconds=900,
        )
    except MediaStorageError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
        ) from None
    return RedirectResponse(url, status_code=307)


@router.post("/conversations/{conversation_id}/note")
def add_internal_note(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    note: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    clean_note = note.strip()
    if clean_note:
        message = register_message(
            db,
            conversation,
            direction="INTERNAL",
            message_type="NOTE",
            body=clean_note,
            sender_user_id=user.id,
            status="INTERNAL",
        )
        write_audit(
            db,
            user=user,
            action="CONVERSATION_NOTE_ADDED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={"message_id": message.id},
        )
        db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/assign")
def assign_conversation(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    assigned_user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    eligible = {item.id: item for item in _eligible_users(db, user, conversation)}
    target = eligible.get(assigned_user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    previous = conversation.assigned_user_id
    conversation.assigned_user_id = target.id
    conversation.status = "ATRIBUIDO" if conversation.status == "EM_FILA" else conversation.status
    if conversation.lead:
        conversation.lead.assigned_user_id = target.id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=target.id,
            assigned_by_id=user.id,
            event="TRANSFERRED" if previous else "ASSIGNED",
        )
    )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_ASSIGNED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"from_user_id": previous, "to_user_id": target.id},
    )
    db.commit()
    online = is_user_whatsapp_online(db, target.id)
    flash(
        request,
        f"Conversa transferida para {target.name}."
        + ("" if online else " Ele está offline e verá a conversa quando entrar no WhatsApp."),
    )
    if user.role == "CONSULTOR":
        return RedirectResponse("/conversations?view=active", status_code=303)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


def _after_queue_transfer_redirect(user: User, conversation: Conversation) -> RedirectResponse:
    if user.role == "CONSULTOR":
        return RedirectResponse("/conversations?view=active", status_code=303)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/transfer-queue")
def transfer_conversation_queue(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    queue_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current = queue_for_conversation(db, conversation.id)
    allowed = {item["id"] for item in _transfer_queues(db, user, current)}
    queue = get_queue(db, queue_id)
    if not queue or not queue.active or queue.id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    result = _move_conversation_to_queue(
        db,
        actor=user,
        conversation=conversation,
        queue=queue,
        reason="QUEUE",
    )
    db.commit()
    if result["ai_job"]:
        flash(request, f"Conversa enviada para a fila {queue.name}. A IA vai responder o cliente agora.")
    elif result["assigned"]:
        flash(request, f"Conversa enviada para a fila {queue.name} e atribuída a {result['assigned'].name}.")
    else:
        flash(request, f"Conversa enviada para a fila {queue.name}. Ela fica na fila até alguém assumir.")
    return _after_queue_transfer_redirect(user, conversation)


@router.post("/conversations/{conversation_id}/transfer-ai")
def transfer_conversation_to_ai(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    queue = ai_transfer_queue(db, conversation.id)
    if not queue:
        flash(request, "Nenhuma fila está com a IA ativa. Ative a IA em uma fila antes de transferir.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)

    result = _move_conversation_to_queue(
        db,
        actor=user,
        conversation=conversation,
        queue=queue,
        reason="AI",
    )
    db.commit()
    if result["ai_job"]:
        flash(request, f"Conversa passada para a IA (fila {queue.name}). Ela vai responder o cliente agora.")
    elif result["pending_message"]:
        flash(
            request,
            f"Conversa movida para a fila {queue.name}, mas a IA não pôde assumir "
            "(cliente pediu para sair ou atendimento já concluído). Atribua a um vendedor.",
            "warning",
        )
    else:
        flash(
            request,
            f"Conversa passada para a IA (fila {queue.name}). Não há mensagem do cliente pendente; "
            "a IA responde na próxima mensagem ou no follow-up.",
        )
    return _after_queue_transfer_redirect(user, conversation)


@router.post("/conversations/{conversation_id}/status")
def update_conversation_status(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    conversation_status: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    allowed = {
        "EM_ATENDIMENTO",
        "AGUARDANDO_CLIENTE",
        "AGUARDANDO_INTERNO",
        "FOLLOW_UP",
        "RESOLVIDO",
        "ENCERRADO",
    }
    if conversation_status not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    conversation.status = conversation_status
    conversation.closed_at = utcnow() if conversation_status in CLOSED_CONVERSATION_STATUSES else None
    write_audit(
        db,
        user=user,
        action="CONVERSATION_STATUS_CHANGED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"status": conversation_status},
    )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/ai/resume")
def resume_conversation_closer(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    from app.services.ai_seller import resume_closer

    try:
        resumed = resume_closer(db, conversation, user)
    except Exception:
        db.rollback()
        resumed = False
    if resumed:
        db.commit()
        flash(request, "Venda devolvida ao Vendedor IA.")
    else:
        flash(request, "Esta conversa não tem venda do Vendedor IA para retomar.", "warning")
    return RedirectResponse(f"/conversations/{conversation_id}", status_code=303)


@router.get("/api/conversations/inbox")
def conversation_inbox_api(
    request: Request,
    q: str = "",
    view: str = "active",
    assignee: int | None = None,
    queue: int | None = None,
    tag: str = "",
    temperature: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entre no WhatsApp para acessar o canal.",
        )
    queues = _queue_panel(db, user) if queue is not None else []
    _validate_queue_filter(user, queue, queues)
    conversations = _inbox_query(
        db, user, q=q, view=view, assignee_id=assignee, queue_id=queue,
        tag_code=tag, temperature=temperature,
    )
    ids = [item.id for item in conversations]
    tags = tag_map(db, ids)
    insights = insight_map(db, ids)
    return {
        "counts": _inbox_counts(
            db, user, queue_id=queue, assignee_id=assignee,
            tag_code=tag, temperature=temperature,
        ),
        "items": [
            {
                "id": item.id,
                "name": (
                    item.contact.business_name
                    or item.contact.name
                    or item.contact.phone
                ),
                "phone": item.contact.phone,
                "preview": item.last_message_preview or "Conversa iniciada",
                "owner": item.assigned_user.name if item.assigned_user else "Fila sem responsável",
                "status": item.status,
                "unread_count": int(item.unread_count or 0),
                "last_message_at": item.last_message_at.isoformat() if item.last_message_at else None,
                "commercial_tags": tags.get(item.id, []),
                "commercial_temperature": (
                    insights[item.id].temperature if item.id in insights else "INDEFINIDO"
                ),
                "commercial_score": (
                    insights[item.id].score if item.id in insights else None
                ),
            }
            for item in conversations
        ],
    }


@router.get("/api/conversations/{conversation_id}/messages")
def conversation_messages_api(
    conversation_id: int,
    request: Request,
    after_id: int = 0,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entre no WhatsApp para acessar o canal.",
        )
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    stmt = (
        select(Message)
        .where(
            Message.contact_id == conversation.contact_id,
            Message.id > max(after_id, 0),
        )
        .options(selectinload(Message.sender_user))
        .order_by(Message.id.asc())
    )
    items = list(db.scalars(stmt))
    if conversation.unread_count:
        mark_conversation_read(db, conversation)
        db.commit()
    location_urls = location_urls_for(db, items)
    return {
        "conversation_id": conversation.id,
        "items": [
            {
                "id": item.id,
                "map_url": location_urls.get(item.id),
                "direction": item.direction,
                "type": item.message_type,
                "body": item.body,
                "media_url": f"/conversations/messages/{item.id}/media" if item.media_url else None,
                "mime_type": item.mime_type,
                "status": item.status,
                "sender": item.sender_user.name if item.sender_user else ("Cliente" if item.direction == "INBOUND" else "Sulnet"),
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ],
    }



@router.post("/conversations/{conversation_id}/register-crm")
def register_crm_manually(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    client_name: str = Form(...),
    db: Session = Depends(get_db),
):
    """Registro humano e explícito; não executa IA nem envia WhatsApp."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return _conversation_unavailable(request)
    try:
        result = register_conversation(
            db,
            conversation=conversation,
            actor=user,
            client_name=client_name,
        )
    except CRMManualError as exc:
        db.rollback()
        flash(request, str(exc), "warning")
    except Exception:
        db.rollback()
        # Nunca mostrar chaves de integração ou detalhes internos no navegador.
        flash(request, "Falha inesperada na integração. Nenhum novo envio deve ser repetido sem conferência no CRM.", "warning")
        raise
    else:
        message = (
            "Este contato já estava registrado no CRM."
            if result.get("already") else "Oportunidade registrada no CRM."
        )
        if result.get("protocol"):
            message += f" Protocolo: {result['protocol']}."
        flash(request, message, "success")
    return RedirectResponse(f"/conversations/{conversation_id}", status_code=303)


@router.post("/conversations/{conversation_id}/blacklist")
def blacklist_conversation(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    reason: str = Form("Cliente solicitou não receber mais mensagens."),
    db: Session = Depends(get_db),
):
    """Vendedor pode bloquear; remoção é exclusiva do administrador."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return _conversation_unavailable(request)
    try:
        entry, created = block_phone(
            db,
            phone=conversation.contact.phone,
            name=conversation.contact.business_name or conversation.contact.name,
            reason=reason or "Cliente solicitou não receber mensagens",
            source="CONVERSA",
            actor_id=user.id,
        )
    except BlacklistPhoneError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    write_audit(
        db, user=user, action="GLOBAL_BLACKLIST_ADDED_FROM_CONVERSATION",
        entity_type="global_contact_blacklist", entity_id=entry.id,
        payload={"conversation_id": conversation.id, "created": created},
    )
    db.commit()
    flash(
        request,
        "Contato bloqueado globalmente: nenhuma fila ou instância poderá enviar mensagens."
        if created else "Este número já está bloqueado em todas as filas.",
        "success",
    )
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/commercial-tag")
def update_commercial_tag(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    code: str = Form(...),
    enabled: str = Form(...),
    db: Session = Depends(get_db),
):
    """Operação manual, apenas etiquetas internas; nunca dispara WhatsApp."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa indisponível.")
    if code not in TAG_LABELS or enabled not in {"0", "1"}:
        raise HTTPException(status_code=400, detail="Etiqueta ou ação inválida.")
    update_manual_tag(
        db, conversation_id=conversation.id, code=code,
        enabled=(enabled == "1"), changed_by_id=user.id,
    )
    write_audit(
        db, user=user, action="COMMERCIAL_TAG_MANUAL_UPDATED",
        entity_type="conversation", entity_id=conversation.id,
        payload={"code": code, "enabled": enabled == "1"},
    )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.get("/conversations-intelligence")
def commercial_heatmap(
    request: Request,
    queue: int | None = None,
    db: Session = Depends(get_db),
):
    """Mapa de calor agregado, com controle de visibilidade gerencial."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR")
    queues = _queue_panel(db, user)
    _validate_queue_filter(user, queue, queues)
    scoped_stmt = apply_conversation_scope(
        select(Conversation).options(selectinload(Conversation.assigned_user)),
        user, inbox=True,
    ).where(Conversation.status.in_(OPEN_CONVERSATION_STATUSES))
    if queue is not None:
        scoped_stmt = scoped_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id == queue)
    conversations = list(db.scalars(scoped_stmt))
    ids = [row.id for row in conversations]
    insights = insight_map(db, ids)
    tags = tag_map(db, ids)
    by_owner: dict[int, dict] = {}
    total = {"QUENTE": 0, "MORNO": 0, "FRIO": 0, "INDEFINIDO": 0}
    pending = 0
    high_potential = 0
    tagged_counts = {code: 0 for code in TAG_LABELS}
    for item in conversations:
        temp = insights[item.id].temperature if item.id in insights else "INDEFINIDO"
        if temp not in total:
            temp = "INDEFINIDO"
        total[temp] += 1
        owner_id = item.assigned_user_id or 0
        row = by_owner.setdefault(
            owner_id,
            {
                "id": item.assigned_user_id,
                "name": (item.assigned_user.name if item.assigned_user else "Sem responsável"),
                "QUENTE": 0, "MORNO": 0, "FRIO": 0, "INDEFINIDO": 0,
                "pending": 0,
            },
        )
        row[temp] += 1
        if _needs_attention(item.status, item.unread_count, item.assigned_user_id):
            pending += 1
            row["pending"] += 1
        codes = {tag["code"] for tag in tags.get(item.id, [])}
        for code in codes:
            if code in tagged_counts:
                tagged_counts[code] += 1
        if "ALTO_POTENCIAL" in codes or "EM_FECHAMENTO" in codes:
            high_potential += 1
    sorted_rows = sorted(
        by_owner.values(),
        key=lambda row: (-row["QUENTE"], -row["MORNO"], row["name"]),
    )
    sorted_tags = [
        {"code": code, "name": TAG_LABELS[code], "count": count}
        for code, count in sorted(tagged_counts.items(), key=lambda pair: -pair[1])
        if count
    ]
    return render(
        request, "conversations/heatmap.html",
        user=user,
        active_nav="conversations",
        page_title="Inteligência comercial",
        queues=queues, selected_queue=queue,
        rows=sorted_rows, totals=total, pending=pending,
        high_potential=high_potential,
        total_conversations=len(conversations),
        tag_totals=sorted_tags,
        temperature_catalog=TEMPERATURE_LABELS,
    )
