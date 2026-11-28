from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import (
    Activity,
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIReplyDelivery,
    AIRun,
    Contact,
    Conversation,
    ConversationAssignment,
    ConversationQueueLink,
    ConversationZApiInstance,
    ConversationMetaAccount,
    ConversationCommercialInsight,
    ConversationCommercialTag,
    Lead,
    Message,
    ServiceQueue,
    User,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.access_control import has_permission
from app.services.ai_reply_delivery import reply_statuses
from app.services.ai_queue_scope import (
    AI_RESUMED_EVENT, HUMAN_TAKEOVER_EVENT, human_takeover_locked, is_dedicated_ai_queue,
)
from app.services.ai_followups import cancel_pending_followups
from app.services.ai_sdr import AI_INBOUND_MESSAGE_TYPES, enqueue_ai_job, latest_run_for_conversation
from app.services.audit import write_audit
from app.services.campaign_queue import (
    list_quick_chat_templates,
    personalize_quick_chat_template,
    resolve_quick_chat_template,
)
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
    exclusive_conversation_access_clause,
    location_urls_for,
    outbound_without_reply_clause,
    conversation_visible_to_user,
    ensure_contact,
    ensure_open_conversation,
    find_scoped_whatsapp_alias,
    mark_conversation_read,
    register_message,
)
from app.services.crm_credit_contestation import (
    MAX_FILE_SIZE as CONTEST_MAX_FILE_SIZE,
    get_contestation_panel,
    send_contestation,
)
from app.services.crm_manual_credit import (
    consult_registered_cpf,
    credit_status as manual_credit_status,
    refresh_credit_status_from_crm,
)
from app.services import crm_prospect_sync
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
from app.services.origin_campaigns import resolve_conversation_origin
from app.services.crm_manual_registration import (
    ACTION_REGISTERED,
    CRMManualError,
    _as_int,
    register_conversation,
    registration_status,
    seller_for_user,
)
from app.services.instance_privacy import (
    require_instance_access, protected_conversation, may_access_instance,
)
from app.services.media_processing import MediaProcessingError, normalize_audio
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.meta_images import archive_meta_image, meta_image_ids
from app.services.metrics import apply_user_scope
from app.services.pii import redact_pii_text
from app.services.scoring import normalize_phone
from app.services.upload_security import UploadValidationError, validate_upload
from app.services.queue_routing import (
    MANUAL_AI_ORIGIN,
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
from app.services.paced_outbound import needs_pacing, queue_manual_message
from app.services.meta_whatsapp import (
    MetaWhatsAppError, default_queue_for_meta_account, is_digital_queue,
    is_official_digital, queue_meta_account, service_for_meta_account,
)
from app.routers.meta_campaigns import _list as list_official_templates
from app.services.manual_meta_templates import eligible_templates, template_payload
from app.services.zapi import ZApiError
from app.services.zapi_instances import (
    bind_conversation_instance,
    conversation_instance,
    queue_instance,
    service_for_conversation,
)
from app.timeutils import utc_naive, utcnow
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
    conversation = db.scalar(_conversation_stmt(user).where(Conversation.id == conversation_id))
    if conversation:
        return conversation
    # Dono direto ainda enxerga a conversa que iniciou/assumiu, mesmo quando o
    # filtro de inbox esconde disparos sem resposta do cliente.
    return db.scalar(
        apply_conversation_scope(
            select(Conversation)
            .options(
                selectinload(Conversation.contact),
                selectinload(Conversation.lead),
                selectinload(Conversation.assigned_user),
                selectinload(Conversation.branch),
            )
            .where(
                Conversation.id == conversation_id,
                Conversation.assigned_user_id == user.id,
            ),
            user,
            inbox=False,
        )
    )


def _assign_quick_start_owner(db: Session, conversation: Conversation, user: User) -> bool:
    """Garante que quem abriu Nova conversa fica gravado como responsável.

    Retorna True quando houve claim (conversa estava sem dono).
    """
    claimed = conversation.assigned_user_id is None
    conversation.assigned_user_id = user.id
    if claimed or conversation.status in {"EM_FILA", "NOVO", "AGUARDANDO_CLIENTE"}:
        conversation.status = "ATRIBUIDO"
    if conversation.source.startswith(("DISPARO:", "CAMPANHA:")):
        # Abertura humana explícita devolve a conversa à caixa do vendedor.
        conversation.source = "MANUAL"
    lead = conversation.lead
    if lead is None and conversation.lead_id:
        lead = db.get(Lead, conversation.lead_id)
    if lead is not None and lead.assigned_user_id != user.id:
        lead.assigned_user_id = user.id
    if claimed:
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=user.id,
                assigned_by_id=user.id,
                event="CLAIMED",
            )
        )
    return claimed


def _claim_on_human_reply(
    db: Session,
    conversation: Conversation,
    user: User,
    *,
    event: str = "CLAIMED_REPLY",
) -> bool:
    """Se o vendedor abre/responde uma conversa sem dono, ela fica gravada com ele.

    Evita o caso em que a Claudia escreve, a resposta do cliente cai na fila e
    outro vendedor (ex.: Maria) aparece como responsável.
    """
    if conversation.assigned_user_id is not None:
        return False
    conversation.assigned_user_id = user.id
    if conversation.status in {"EM_FILA", "NOVO", "AGUARDANDO_CLIENTE", "EM_ATENDIMENTO"}:
        conversation.status = "ATRIBUIDO"
    lead = conversation.lead
    if lead is None and conversation.lead_id:
        lead = db.get(Lead, conversation.lead_id)
    if lead is not None and lead.assigned_user_id != user.id:
        lead.assigned_user_id = user.id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=user.id,
            assigned_by_id=user.id,
            event=event,
        )
    )
    return True


def _may_read_full_conversation_history(user: User, conversation: Conversation) -> bool:
    """Somente a gestao e o responsavel autorizado veem todo este atendimento."""
    return user.role in {"ADMIN", "GESTOR"} or conversation.assigned_user_id == user.id


def _history_since(db: Session, user: User, conversation: Conversation):
    """O responsavel recebe o historico deste atendimento ao assumir a conversa.

    Chamadores devem aplicar o escopo de acesso antes de consultar mensagens.
    Nao utilizar contact_id ou telefone para ampliar o escopo do inbox.
    Outros atendimentos e o historico global continuam restritos.
    """
    if _may_read_full_conversation_history(user, conversation):
        return None
    return db.scalar(
        select(ConversationAssignment.created_at)
        .where(
            ConversationAssignment.conversation_id == conversation.id,
            ConversationAssignment.event == "TRANSFERRED",
        )
        .order_by(
            ConversationAssignment.created_at.desc(),
            ConversationAssignment.id.desc(),
        )
        .limit(1)
    )


def _inbox_privacy_cutoffs(
    db: Session, user: User, conversations: list[Conversation],
) -> dict[int, object]:
    """Uma consulta para mascarar previews antigos de conversas transferidas."""
    if user.role in {"ADMIN", "GESTOR"} or not conversations:
        return {}
    ids = [item.id for item in conversations]
    rows = db.execute(
        select(
            ConversationAssignment.conversation_id,
            func.max(ConversationAssignment.created_at),
        )
        .where(
            ConversationAssignment.conversation_id.in_(ids),
            ConversationAssignment.event == "TRANSFERRED",
        )
        .group_by(ConversationAssignment.conversation_id)
    )
    return {conversation_id: cutoff for conversation_id, cutoff in rows}


def _private_preview(conversation: Conversation, cutoff=None) -> str:
    if cutoff is not None and (
        conversation.last_message_at is None
        or utc_naive(conversation.last_message_at) < utc_naive(cutoff)
    ):
        return "Atendimento transferido; historico anterior restrito"
    return conversation.last_message_preview or "Conversa iniciada"


def _conversation_messages(db: Session, conversation: Conversation, since=None) -> list[Message]:
    stmt = select(Message).where(Message.conversation_id == conversation.id)
    if since is not None:
        stmt = stmt.where(Message.created_at >= since)
    return list(
        db.scalars(
            stmt.options(selectinload(Message.sender_user))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )


def _conversation_transfer_events(
    db: Session, conversation: Conversation, since=None,
) -> list[ConversationAssignment]:
    stmt = select(ConversationAssignment).where(
        ConversationAssignment.conversation_id == conversation.id,
        ConversationAssignment.event == "TRANSFERRED",
    )
    if since is not None:
        stmt = stmt.where(ConversationAssignment.created_at >= since)
    return list(
        db.scalars(
            stmt.options(
                selectinload(ConversationAssignment.user),
                selectinload(ConversationAssignment.assigned_by),
            )
            .order_by(ConversationAssignment.created_at.asc(), ConversationAssignment.id.asc())
        )
    )


def _message_visible_in_conversation(
    db: Session, user: User, message: Message,
) -> bool:
    conversation = _get_conversation(db, user, message.conversation_id)
    if conversation is None:
        return False
    since = _history_since(db, user, conversation)
    if since is None:
        return True
    return utc_naive(message.created_at) >= utc_naive(since)


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
    """Última mensagem do cliente sem resposta, inclusive mídias suportadas."""
    latest = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction.in_(("INBOUND", "OUTBOUND")),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    if not latest or latest.direction != "INBOUND" or latest.message_type not in AI_INBOUND_MESSAGE_TYPES:
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
    """Transfere fila ou delega uma conversa a IA sem trocar seu numero de saida."""
    previous_queue = queue_for_conversation(db, conversation.id)
    previous_user_id = conversation.assigned_user_id
    if reason == "AI" and previous_queue and not conversation_instance(db, conversation.id):
        instance = queue_instance(db, previous_queue.id)
        if instance:
            bind_conversation_instance(db, conversation.id, instance.id)
    origin = MANUAL_AI_ORIGIN if reason == "AI" else MANUAL_TRANSFER_ORIGIN
    bind_conversation_queue(db, conversation.id, queue.id, origin=origin)
    conversation.team = queue.team
    conversation.assigned_user_id = None
    if conversation.status not in CLOSED_CONVERSATION_STATUSES:
        conversation.status = "EM_FILA"

    result = {"ai_job": None, "assigned": None, "pending_message": None}
    ai_queue = ai_transfer_queue(db) if reason == "AI" else None
    binding = usable_ai_binding(db, ai_queue.id if ai_queue else queue.id) if (ai_queue or reason != "AI") else None
    if binding and not human_takeover_locked(db, conversation.id):
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
            last_mode = None
            if reason == "AI" and job and job.status == "CONCLUIDO":
                last_mode = db.scalar(
                    select(AIRun.execution_mode).where(AIRun.job_id == job.id)
                    .order_by(AIRun.id.desc()).limit(1)
                )
            if job and (job.status in _RETRYABLE_AI_JOB_STATUSES or last_mode == "SHADOW"):
                # Job dessa mensagem já tinha falhado (ex.: OpenAI sem crédito em
                # 09/11): a transferência manual dá uma nova chance.
                job.status = "PENDENTE"
                job.attempts = 0
                job.locked_at = None
                job.available_at = utcnow()
                job.agent_id = binding.agent_id
                job.queue_id = queue.id
                job.error = f"[transferência manual] {job.error or ''}"[:2000]
            if reason == "AI" and job and job.status == "PENDENTE":
                if job.queue_id != queue.id:
                    job.agent_id = binding.agent_id
                    job.queue_id = queue.id
                job.available_at = utcnow()
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


def _conversation_queue_labels(
    db: Session, conversation_ids: list[int],
) -> dict[int, dict[str, str | int]]:
    """Uma consulta para as filas reais de conversas visíveis; não usa o filtro global.

    A tabela de vínculo tem restrição UNIQUE por conversa. A fila exibida em
    cada linha é sempre a própria fila do atendimento, nunca a fila filtrada.
    """
    if not conversation_ids:
        return {}
    rows = db.execute(
        select(
            ConversationQueueLink.conversation_id,
            ServiceQueue.id,
            ServiceQueue.name,
            ServiceQueue.code,
        )
        .join(ServiceQueue, ServiceQueue.id == ConversationQueueLink.queue_id)
        .where(ConversationQueueLink.conversation_id.in_(conversation_ids))
    )
    return {
        conversation_id: {"id": queue_id, "name": queue_name, "code": queue_code}
        for conversation_id, queue_id, queue_name, queue_code in rows
    }


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
    for assigned_id in db.scalars(apply_conversation_scope(stmt, user)):
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
        .where(exclusive_conversation_access_clause(user))
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


def _manual_outbound_queues(db: Session, user: User) -> list[tuple[ServiceQueue, object]]:
    """Filas autorizadas, com um único provedor ativo e sem fallback."""
    rows = []
    for queue in available_queues_for_user(db, user):
        meta = queue_meta_account(db, queue.id)
        instance = queue_instance(db, queue.id)
        if meta:
            bound = default_queue_for_meta_account(db, meta.id)
            if not instance and bound and bound.id == queue.id and is_official_digital(meta) and is_digital_queue(queue):
                rows.append((queue, meta))
        elif instance and may_access_instance(user, instance):
            rows.append((queue, instance))
    return rows


def _manual_queue_channel(db: Session, queue_id: int) -> str:
    return "META" if queue_meta_account(db, queue_id) else "ZAPI"


@router.get("/conversations/start/meta-templates")
def manual_meta_templates(request: Request, queue_id: int, db: Session = Depends(get_db)):
    user, redirect = _user_or_redirect(request, db)
    if redirect or not has_permission(user, "conversations.reply"):
        return JSONResponse({"error": "Acesso não autorizado."}, status_code=403)
    allowed = dict((q.id, provider) for q, provider in _manual_outbound_queues(db, user))
    if queue_id not in allowed or _manual_queue_channel(db, queue_id) != "META":
        return JSONResponse({"error": "Fila Meta não autorizada."}, status_code=403)
    try:
        rows = eligible_templates(list_official_templates(allowed[queue_id]))
    except (ValueError, ZApiError):
        return JSONResponse({"error": "Não foi possível consultar templates oficiais."}, status_code=502)
    return JSONResponse({"templates": rows}, headers={"Cache-Control": "no-store"})


def _has_meta_link(db: Session, conversation: Conversation) -> bool:
    return db.scalar(select(ConversationMetaAccount.id).where(
        ConversationMetaAccount.conversation_id == conversation.id
    )) is not None


def _manual_meta_start(db, request, user, queue, account, contact, lead,
                       template_id, parameters, consent, wants_json, fail):
    # O vínculo da conta, e não apenas o telefone do cliente, identifica a conversa.
    db.scalar(select(Contact.id).where(Contact.id == contact.id).with_for_update())
    existing = db.scalar(select(Conversation).join(
        ConversationMetaAccount,
        ConversationMetaAccount.conversation_id == Conversation.id,
    ).where(
        Conversation.contact_id == contact.id,
        Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
        ConversationMetaAccount.meta_account_id == account.id,
        Conversation.id.not_in(select(ConversationZApiInstance.conversation_id)),
        Conversation.id.not_in(select(Message.conversation_id).join(ConversationMetaAccount, ConversationMetaAccount.conversation_id == Message.conversation_id).where(Message.created_at < ConversationMetaAccount.created_at, or_(Message.provider_message_id.is_(None), ~Message.provider_message_id.like("wamid.%")))),
    ).order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(1))
    if not existing and db.scalar(select(Conversation.id).join(
        ConversationMetaAccount, ConversationMetaAccount.conversation_id == Conversation.id
    ).where(Conversation.contact_id == contact.id,
            Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            ConversationMetaAccount.meta_account_id == account.id).limit(1)):
        db.rollback()
        return fail("Histórico Meta com vínculo inconsistente. Solicite revisão do gestor.", 409)
    if existing and (existing.assigned_user_id not in (None, user.id)
                     or (existing.assigned_user_id is not None and not _get_conversation(db, user, existing.id))):
        db.rollback()
        return fail("Este número já tem atendimento Meta ativo. Solicite transferência.", 409)
    if existing:
        linked_queue = queue_for_conversation(db, existing.id)
        if not linked_queue or linked_queue.id != queue.id:
            db.rollback()
            return fail("Atendimento Meta vinculado a outra fila. Solicite revisão do gestor.", 409)
    if not template_id and (not existing or needs_pacing(db, existing)):
        db.rollback()
        return fail("Selecione um template aprovado da Meta para iniciar ou retomar o contato.")
    if template_id and not consent:
        db.rollback()
        return fail("Confirme que o cliente autorizou o contato pelo WhatsApp.")
    body, template = "", None
    if template_id:
        try:
            rows = eligible_templates(list_official_templates(account))
            row = next((r for r in rows if r["id"] == template_id), None)
            if not row:
                raise ValueError("Template indisponível ou não aprovado.")
            template, body = template_payload(row, parameters)
        except (ValueError, ZApiError) as exc:
            db.rollback()
            return fail(str(exc))
    conversation = existing or ensure_open_conversation(
        db, contact, lead=lead, source="MANUAL", queue_id=queue.id,
        queue_origin="MANUAL_OUTBOUND", assign_if_unowned=False,
        meta_account_id=account.id,
    )
    # A engine pode encontrar uma conversa concorrente; nunca roubar titularidade.
    if conversation.assigned_user_id not in (None, user.id):
        db.rollback()
        return fail("Atendimento já atribuído a outro vendedor.", 409)
    # Ownership is per conversation; do not change an unrelated CRM lead owner.
    if conversation.assigned_user_id is None:
        conversation.assigned_user_id = user.id
        conversation.status = "ATRIBUIDO"
        db.add(ConversationAssignment(conversation_id=conversation.id, user_id=user.id,
                                      assigned_by_id=user.id, event="CLAIMED"))
    sent = False
    if template:
        try:
            service = service_for_meta_account(account)
            result = service.send_template(phone=contact.phone, template=template)
            provider_id = result.get("messageId") or result.get("id")
            if not provider_id:
                raise MetaWhatsAppError("Meta não confirmou a mensagem. Não reenvie automaticamente.")
        except ZApiError as exc:
            db.rollback()
            return fail(str(exc), 502)
        register_message(db, conversation, direction="OUTBOUND", message_type="TEXT",
                         body=body, sender_user_id=user.id, provider_message_id=str(provider_id),
                         status="SENT")
        if lead:
            lead.last_contact_at = utcnow()
        sent = True
    write_audit(db, user=user, action="CONVERSATION_QUICK_STARTED",
                entity_type="conversation", entity_id=conversation.id,
                payload={"queue_id": queue.id, "meta_account_id": account.id,
                         "provider": "META", "template_id": template_id or None,
                         "consent_confirmed": bool(consent), "message_sent": sent,
                         "owner_user_id": user.id})
    db.commit()
    notice = "Template oficial enviado." if sent else "Conversa Meta aberta."
    if wants_json:
        return JSONResponse({"ok": True, "conversation_id": conversation.id,
            "reused": bool(existing), "phone": contact.phone, "name": contact.name or contact.phone,
            "queue_name": queue.name, "owner": user.name, "status": conversation.status,
            "draft": "", "message_sent": sent, "send_status": "SENT" if sent else None,
            "notice": notice})
    flash(request, notice, "success")
    return RedirectResponse(f"/conversations/{conversation.id}?quick=1", status_code=303)


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
    template_id: str = "",
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
    transfer_history: list[ConversationAssignment] = []
    history_timeline: list[dict] = []
    users: list[User] = []
    selected_queue = None
    latest_ai_run = None
    if selected:
        selected_queue = queue_for_conversation(db, selected.id)
        latest_ai_run = latest_run_for_conversation(db, selected.id)
        # Abrir conversa sem dono na fila MANUAL assume o atendimento.
        # Sem isso, marcar como lida zera o unread e a conversa some da lista
        # antes do vendedor conseguir responder.
        if selected.assigned_user_id is None and user.role == "CONSULTOR":
            _claim_on_human_reply(db, selected, user, event="CLAIMED_OPEN")
            db.commit()
        if selected.unread_count:
            mark_conversation_read(db, selected)
            db.commit()
        since = _history_since(db, user, selected)
        messages = _conversation_messages(db, selected, since)
        transfer_history = _conversation_transfer_events(db, selected, since)
        history_timeline = [
            {"kind": "message", "at": msg.created_at, "id": msg.id, "message": msg}
            for msg in messages
        ] + [
            {"kind": "transfer", "at": assignment.created_at, "id": assignment.id,
             "transfer": assignment}
            for assignment in transfer_history
            if assignment.event == "TRANSFERRED"
        ]
        history_timeline.sort(key=lambda item: (item["at"], item["kind"], item["id"]))
        users = [] if protected_conversation(db, selected.id) else _eligible_users(db, user, selected)
        transfer_online_ids = set(online_user_ids(db))
        users.sort(key=lambda item: (item.id not in transfer_online_ids, item.name or ""))
        ai_queue = ai_transfer_queue(db)
        transfer_queues = [] if _has_meta_link(db, selected) else [
            item for item in _transfer_queues(db, user, selected_queue)
            if not queue_meta_account(db, item["id"])
        ]
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
    conversation_queue_labels = _conversation_queue_labels(db, visible_ids)
    manual_ai_ids = set(db.scalars(
        select(ConversationQueueLink.conversation_id).where(
            ConversationQueueLink.conversation_id.in_(visible_ids),
            ConversationQueueLink.origin == MANUAL_AI_ORIGIN,
        )
    ))
    ai_reply_statuses = reply_statuses(
        db, [
            cid for cid, queue in conversation_queue_labels.items()
            if queue["code"] == "FILA-IA" or cid in manual_ai_ids
        ],
    )
    commercial_tags = tag_map(db, visible_ids)
    commercial_insights = insight_map(db, visible_ids)
    privacy_cutoffs = _inbox_privacy_cutoffs(
        db, user, conversations + ([selected] if selected and selected.id not in
                                    {item.id for item in conversations} else []),
    )
    # Classificacoes/resumos anteriores a transferencia nao sao contexto do novo vendedor.
    for private_id in privacy_cutoffs:
        commercial_tags.pop(private_id, None)
        commercial_insights.pop(private_id, None)
    if selected and selected.id in privacy_cutoffs:
        latest_ai_run = None
    quick_queues = (
        _manual_outbound_queues(db, user)
        if has_permission(user, "conversations.reply") else []
    )
    quick_queue_ids = {queue.id for queue, _ in quick_queues}
    quick_preferred_queue_id = next(
        (
            item_id for item_id in (
                queue_id,
                selected_queue.id if selected_queue else None,
            )
            if item_id in quick_queue_ids
        ),
        quick_queues[0][0].id if quick_queues else None,
    )
    quick_chat_templates = (
        list_quick_chat_templates(db, user)
        if has_permission(user, "conversations.reply") else []
    )
    compose_draft = str(request.session.pop("quick_compose_draft", "") or "")
    # Fallback: resolve template_id da URL caso a sessão não tenha guardado o rascunho.
    if not compose_draft and template_id and selected:
        compose_draft = _quick_compose_draft(
            db, user, template_id=template_id,
            lead=selected.lead, contact=selected.contact,
        )
    crm_seller = seller_for_user(selected.assigned_user) if selected else None
    crm_ai_owner = bool(
        selected and selected.assigned_user_id and db.scalar(
            select(AIAgentProfile.id).where(
                AIAgentProfile.user_id == selected.assigned_user_id,
                AIAgentProfile.active.is_(True),
            ).limit(1)
        )
    )
    crm_ai_opportunity_id = (
        crm_prospect_sync.ai_crm_opportunity_id(db, selected.id)
        if selected and crm_ai_owner else None
    )
    crm_registration = (
        registration_status(db, selected.contact_id, crm_seller.username)
        if selected and crm_seller else None
    )
    crm_opportunity_id = (
        _as_int(crm_registration.get("opportunityId"))
        if crm_registration and crm_registration["state"] == ACTION_REGISTERED else None
    )
    crm_credit = (
        manual_credit_status(db, selected.contact_id, crm_seller.username, crm_opportunity_id)
        if selected and crm_seller and crm_opportunity_id else None
    )
    crm_origin_label = "Sulnet Prospecta"
    crm_origin_match = None
    if selected and crm_seller and not crm_registration and not crm_ai_opportunity_id:
        crm_origin_label, crm_origin_match = resolve_conversation_origin(db, selected.id)
    return render(
        request,
        "conversations/inbox.html",
        closer_escalated=closer_escalated,
        user=user,
        active_nav="conversations",
        page_title="Conversas",
        conversations=conversations,
        conversation_previews={
            item.id: _private_preview(
                item, None if _may_read_full_conversation_history(user, item)
                else privacy_cutoffs.get(item.id),
            )
            for item in conversations
        },
        conversation=selected,
        human_takeover_active=bool(selected and human_takeover_locked(db, selected.id)),
        messages=messages,
        transfer_history=transfer_history,
        history_timeline=history_timeline,
        location_urls=location_urls_for(db, messages),
        meta_image_ids=meta_image_ids(db, messages),
        users=users,
        transfer_online_ids=transfer_online_ids,
        ai_transfer_queue=ai_queue,
        transfer_queues=transfer_queues,
        selected_queue=selected_queue,
        conversation_queue_labels=conversation_queue_labels,
        ai_reply_statuses=ai_reply_statuses,
        quick_outbound_queues=[q for q, _instance in quick_queues],
        quick_queue_channels={q.id: _manual_queue_channel(db, q.id) for q, _ in quick_queues},
        quick_preferred_queue_id=quick_preferred_queue_id,
        quick_chat_templates=quick_chat_templates,
        compose_draft=compose_draft,
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
        crm_manual_seller=crm_seller,
        crm_manual_status=crm_registration,
        crm_ai_owner=crm_ai_owner,
        crm_ai_opportunity_id=crm_ai_opportunity_id,
        crm_origin_label=crm_origin_label,
        crm_origin_match=crm_origin_match,
        crm_credit_enabled=settings.sulnet_crm_manual_credit_enabled,
        crm_credit_status=crm_credit,
        crm_contestation_enabled=settings.sulnet_crm_contestation_enabled,
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
    template_id: str = "",
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
        template_id=template_id,
    )


def _quick_compose_draft(
    db: Session,
    user: User,
    *,
    template_id: str | None,
    lead: Lead | None,
    contact: Contact | None,
) -> str:
    """Monta o texto do template escolhido (vazio = NENHUM)."""
    raw = resolve_quick_chat_template(db, user, template_id)
    if not raw:
        return ""
    return personalize_quick_chat_template(
        raw,
        lead=lead,
        contact_name=(contact.name if contact else "") or "",
        business_name=(contact.business_name if contact else "") or "",
    )


def _send_quick_template_message(
    db: Session,
    *,
    conversation: Conversation,
    user: User,
    body: str,
) -> tuple[str, str | None]:
    """Envia (ou enfileira com pacing) a mensagem do template escolhido.

    Retorna (status, erro). status: SENT | PENDING | "".
    """
    clean = (body or "").strip()
    if not clean:
        return "", None
    if is_globally_blocked(db, conversation.contact.phone):
        return "", "Contato na blacklist."
    if conversation.lead and (
        conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR"
    ):
        return "", "Cliente não autoriza contato."
    if conversation.source != "TELEGRAM" and needs_pacing(db, conversation):
        try:
            queue_manual_message(
                db, conversation, message_type="TEXT", body=clean,
                sender_user_id=user.id,
            )
        except (ZApiError, ValueError) as exc:
            return "", str(exc)
        write_audit(
            db, user=user, action="CONVERSATION_MESSAGE_QUEUED",
            entity_type="conversation", entity_id=conversation.id,
            payload={"source": "quick_template", "status": "PENDING"},
        )
        return "PENDING", None
    try:
        result = service_for_conversation(db, conversation.id).send_text(
            phone=conversation.contact.phone,
            message=clean,
        )
    except ZApiError as exc:
        return "", str(exc)
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    sent = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=clean,
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
                    "Mensagem de template na Nova conversa: "
                    + redact_pii_text(clean)[:500]
                ),
                completed_at=utcnow(),
            )
        )
    write_audit(
        db, user=user, action="CONVERSATION_MESSAGE_SENT",
        entity_type="conversation", entity_id=conversation.id,
        payload={
            "message_id": sent.id,
            "provider_message_id": provider_id,
            "source": "quick_template",
        },
    )
    return "SENT", None


@router.post("/conversations/start")
def quick_start_whatsapp_conversation(
    request: Request,
    csrf_token: str = Form(...),
    phone: str = Form(...),
    queue_id: int | None = Form(None),
    template_id: str = Form(""),
    meta_template_id: str = Form(""),
    meta_parameters: str = Form(""),
    meta_consent: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Inicia chat manual por telefone, preservando fila, instancia e titularidade.

    Formulario tradicional continua redirecionando ao inbox. A interface
    Prospecta Conversas solicita JSON e abre apenas o chat autorizado.
    Template e opcional (NENHUM): sem mensagem. Com campanha selecionada,
    a mensagem da abordagem/regua e enviada (ou enfileirada com pacing).
    """
    wants_json = "application/json" in request.headers.get("accept", "").lower()

    def fail(message: str, code: int = 400):
        # Keep provider/customer text out of production logs; classify known rejections.
        reason = (
            "other_owner" if "outro atendente" in message or "outro vendedor" in message else
            "blacklist" if "blacklist" in message else
            "opt_out" if "não autoriza contato" in message else
            "invalid_phone" if "telefone válido" in message else
            "binding_conflict" if "inconsistente" in message or "outra fila" in message else
            "channel_fields" if "não podem ser enviados" in message else
            "queue_permission" if "fila WhatsApp" in message else
            "template_or_other"
        )
        logging.getLogger(__name__).warning(
            "manual_start_rejected user_id=%s queue_id=%s status=%s reason=%s",
            getattr(user, "id", None), queue_id, code, reason,
        )
        if wants_json:
            return JSONResponse({"ok": False, "error": message}, status_code=code)
        flash(request, message, "warning")
        return RedirectResponse("/conversations", status_code=303)

    user, redirect = _user_or_redirect(request, db)
    if redirect:
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "Entre no atendimento antes de iniciar uma conversa."},
                status_code=401 if redirect.headers.get("location") == "/login" else 403,
            )
        return redirect
    validate_csrf(request, csrf_token)
    if not has_permission(user, "conversations.reply"):
        raise HTTPException(status_code=403, detail="Sem permissão para iniciar conversas.")

    queues = _manual_outbound_queues(db, user)
    if not queues:
        return fail("Nenhuma fila WhatsApp ativa está disponível para você.", 403)
    allowed = {queue.id: (queue, instance) for queue, instance in queues}
    if queue_id is None and len(queues) == 1:
        queue_id = queues[0][0].id
    if queue_id not in allowed:
        return fail("Selecione uma fila WhatsApp autorizada para iniciar a conversa.", 403)
    queue, instance = allowed[queue_id]
    desktop_json = wants_json and request.headers.get("X-Prospecta-Desktop") == "1"
    is_meta = _manual_queue_channel(db, queue.id) == "META"
    if not is_meta:
        require_instance_access(user, instance)
        if meta_template_id or meta_parameters.strip() not in {"", "[]"} or meta_consent:
            return fail("Templates Meta não podem ser enviados pela Z-API.")
    elif template_id:
        return fail("Templates internos não podem ser enviados pela Meta.")

    normalized = normalize_phone(phone)
    if not normalized:
        return fail("Informe um telefone válido com DDD (fixo ou celular).")
    if is_globally_blocked(db, normalized):
        return fail("Este contato está na blacklist e não pode receber mensagens.", 403)

    # Match the alternate Brazilian mobile format only inside the selected
    # provider and queue. The existing owner check below remains mandatory.
    alias_chat = find_scoped_whatsapp_alias(
        db, normalized, queue_id=queue.id,
        meta_account_id=instance.id if is_meta else None,
        zapi_instance_id=None if is_meta else instance.id,
        for_inbound=True,
    )
    existing_contact = db.scalar(select(Contact).where(Contact.phone == normalized))
    lead = db.scalar(select(Lead).where(Lead.phone == normalized).limit(1))
    if not lead and existing_contact and existing_contact.lead_id:
        lead = db.get(Lead, existing_contact.lead_id)
    if lead and (lead.do_not_contact or lead.stage == "NAO_CONTATAR"):
        return fail("Este cliente não autoriza contato por WhatsApp.", 403)
    if alias_chat:
        normalized = alias_chat.contact.phone
        if is_globally_blocked(db, normalized):
            return fail("Este contato está na blacklist e não pode receber mensagens.", 403)
        lead = alias_chat.lead or alias_chat.contact.lead
        if lead and (lead.do_not_contact or lead.stage == "NAO_CONTATAR"):
            return fail("Este cliente não autoriza contato por WhatsApp.", 403)

    contact = ensure_contact(db, normalized, lead=lead)
    if is_meta:
        return _manual_meta_start(db, request, user, queue, instance, contact, lead,
                                  meta_template_id, meta_parameters, meta_consent,
                                  wants_json, fail)
    compose_draft = _quick_compose_draft(
        db, user, template_id=template_id, lead=lead, contact=contact,
    )
    # Only reuse a conversation whose sending number is explicitly known.
    # An unbound legacy chat may be from another channel and must not prevent
    # an authorized seller from opening a conversation on this instance.
    db.scalar(select(Contact.id).where(Contact.id == contact.id).with_for_update())
    existing = alias_chat or db.scalar(
        select(Conversation)
        .join(
            ConversationZApiInstance,
            ConversationZApiInstance.conversation_id == Conversation.id,
        )
        .where(
            Conversation.contact_id == contact.id,
            Conversation.source != "TELEGRAM",
            Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            Conversation.id.not_in(select(ConversationMetaAccount.conversation_id)),
            ConversationZApiInstance.zapi_instance_id == instance.id,
        )
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )
    if existing and existing.assigned_user_id not in (None, user.id):
        db.rollback()
        return fail(
            "Este número já tem uma conversa ativa com outro atendente. "
            "Solicite a transferência para não dividir o atendimento.",
            409,
        )

    existing_queue = queue_for_conversation(db, existing.id) if existing else None
    if existing_queue and queue_meta_account(db, existing_queue.id):
        db.rollback()
        return fail("Vínculo de WhatsApp inconsistente. Solicite revisão do gestor.", 409)
    if existing and existing_queue and existing_queue.id != queue.id:
        if existing.assigned_user_id == user.id:
            # Mesma vendedora: mantém a fila original, mas reforça titularidade e
            # tira do limbo de disparo sem resposta para a conversa continuar na caixa dela.
            _assign_quick_start_owner(db, existing, user)
            send_status, send_error = ("", None)
            if compose_draft:
                send_status, send_error = _send_quick_template_message(
                    db, conversation=existing, user=user, body=compose_draft,
                )
            write_audit(
                db, user=user, action="CONVERSATION_QUICK_STARTED",
                entity_type="conversation", entity_id=existing.id,
                payload={
                    "queue_id": existing_queue.id,
                    "zapi_instance_id": instance.id,
                    "reused": True,
                    "message_sent": send_status in {"SENT", "PENDING"},
                    "send_status": send_status or None,
                    "kept_original_queue": True,
                    "template_id": (template_id or "").strip() or None,
                    "draft_prepared": bool(compose_draft) and not send_status,
                },
            )
            db.commit()
            draft_for_client = "" if send_status else compose_draft
            notice = "Conversa localizada na fila original e mantida com você."
            if send_status == "SENT":
                notice = "Conversa reaberta e mensagem da campanha enviada."
            elif send_status == "PENDING":
                notice = "Conversa reaberta; mensagem da campanha enfileirada (pacing)."
            elif send_error:
                notice = f"{notice} Template não enviado: {send_error}"
            if wants_json:
                if desktop_json:
                    if draft_for_client:
                        request.session["quick_compose_draft"] = draft_for_client
                    flash(request, notice, "warning" if send_error or not send_status else "success")
                return JSONResponse(
                    {
                        "ok": True,
                        "conversation_id": existing.id,
                        "reused": True,
                        "phone": normalized,
                        "name": contact.business_name or contact.name or normalized,
                        "queue_name": existing_queue.name,
                        "owner": user.name,
                        "status": existing.status,
                        "draft": draft_for_client,
                        "message_sent": send_status in {"SENT", "PENDING"},
                        "send_status": send_status or None,
                        "notice": notice,
                    }
                )
            if draft_for_client:
                request.session["quick_compose_draft"] = draft_for_client
            flash(request, notice, "warning" if send_error or not send_status else "success")
            redirect_url = f"/conversations/{existing.id}?quick=1"
            if draft_for_client and (template_id or "").strip():
                redirect_url += f"&template_id={quote((template_id or '').strip(), safe='')}"
            return RedirectResponse(redirect_url, status_code=303)
        db.rollback()
        return fail(
            "Este número já possui conversa ativa em outra fila. "
            "Use a fila correta ou solicite transferência.",
            409,
        )

    if existing:
        conversation = existing
        # Nao deslocar atendimento existente de fila/numero ao abrir o atalho.
        bind_conversation_instance(db, conversation.id, instance.id)
        if not existing_queue:
            bind_conversation_queue(db, conversation.id, queue.id, origin="MANUAL_OUTBOUND")
            conversation.team = queue.team
    else:
        conversation = ensure_open_conversation(
            db, contact, lead=lead, source="MANUAL",
            queue_id=queue.id, queue_origin="MANUAL_OUTBOUND",
            assign_if_unowned=False, zapi_instance_id=instance.id,
            allow_unbound_reuse=False,
        )

    _assign_quick_start_owner(db, conversation, user)
    send_status, send_error = ("", None)
    if compose_draft:
        send_status, send_error = _send_quick_template_message(
            db, conversation=conversation, user=user, body=compose_draft,
        )
    write_audit(
        db, user=user, action="CONVERSATION_QUICK_STARTED",
        entity_type="conversation", entity_id=conversation.id,
        payload={"queue_id": queue_for_conversation(db, conversation.id).id,
                 "zapi_instance_id": instance.id,
                 "reused": bool(existing),
                 "message_sent": send_status in {"SENT", "PENDING"},
                 "send_status": send_status or None,
                 "owner_user_id": user.id,
                 "template_id": (template_id or "").strip() or None,
                 "draft_prepared": bool(compose_draft) and not send_status},
    )
    db.commit()
    draft_for_client = "" if send_status else compose_draft
    if wants_json:
        if desktop_json:
            if draft_for_client:
                request.session["quick_compose_draft"] = draft_for_client
            if send_error:
                flash(request, f"Conversa aberta, mas o template não foi enviado: {send_error}", "warning")
            elif send_status in {"SENT", "PENDING"}:
                flash(request, "Mensagem enviada." if send_status == "SENT" else "Mensagem enfileirada.", "success")
        payload = {
            "ok": True,
            "conversation_id": conversation.id,
            "reused": bool(existing),
            "phone": normalized,
            "name": contact.business_name or contact.name or normalized,
            "queue_name": (existing_queue or queue).name,
            "owner": user.name,
            "status": conversation.status,
            "draft": draft_for_client,
            "message_sent": send_status in {"SENT", "PENDING"},
            "send_status": send_status or None,
        }
        if send_error:
            payload["notice"] = f"Template não enviado: {send_error}"
        elif send_status == "SENT":
            payload["notice"] = "Mensagem da campanha enviada."
        elif send_status == "PENDING":
            payload["notice"] = "Mensagem da campanha enfileirada (pacing)."
        return JSONResponse(payload)
    if draft_for_client:
        request.session["quick_compose_draft"] = draft_for_client
    if send_status == "SENT":
        flash(request, "Mensagem da campanha enviada.", "success")
    elif send_status == "PENDING":
        flash(request, "Mensagem da campanha enfileirada (pacing de 30s).", "success")
    elif send_error:
        flash(request, f"Conversa aberta, mas o template não foi enviado: {send_error}", "warning")
    redirect_url = f"/conversations/{conversation.id}?quick=1"
    if draft_for_client and (template_id or "").strip():
        redirect_url += f"&template_id={quote((template_id or '').strip(), safe='')}"
    return RedirectResponse(redirect_url, status_code=303)


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
    wants_json = "application/json" in request.headers.get("accept", "")
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        if wants_json:
            code = 401 if redirect.headers.get("location") == "/login" else 403
            return JSONResponse(
                {"ok": False, "error": "Entre no atendimento para responder."},
                status_code=code,
            )
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "Conversa não disponível para esta conta."},
                status_code=404,
            )
        return _conversation_unavailable(request)
    if is_globally_blocked(db, conversation.contact.phone) or (conversation.lead and (conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR")):
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "Este contato está bloqueado para novas mensagens."},
                status_code=403,
            )
        flash(request, "Este contato esta bloqueado para novas mensagens.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    if _has_meta_link(db, conversation) and needs_pacing(db, conversation):
        error = "Janela Meta de 24 horas encerrada. Use Nova conversa e um template oficial aprovado."
        if wants_json:
            return JSONResponse({"ok": False, "error": error}, status_code=400)
        flash(request, error, "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    clean_message = message.strip()
    if not clean_message:
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "Digite uma mensagem antes de enviar."},
                status_code=400,
            )
        flash(request, "Digite uma mensagem antes de enviar.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    if conversation.source != "TELEGRAM" and needs_pacing(db, conversation):
        try:
            _claim_on_human_reply(db, conversation, user)
            queue_manual_message(
                db, conversation, message_type="TEXT", body=clean_message,
                sender_user_id=user.id,
            )
            db.commit()
        except (ZApiError, ValueError) as exc:
            db.rollback()
            if wants_json:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
            flash(request, str(exc), "error")
        else:
            if wants_json:
                return JSONResponse({"ok": True, "status": "PENDING"})
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    try:
        result = service_for_conversation(db, conversation.id).send_text(
            phone=conversation.contact.phone,
            message=clean_message,
        )
    except ZApiError as exc:
        if wants_json:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        flash(request, str(exc), "error")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)

    _claim_on_human_reply(db, conversation, user)
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
    if wants_json:
        return JSONResponse({"ok": True, "status": "SENT", "message_id": sent.id})
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

    if _has_meta_link(db, conversation) and needs_pacing(db, conversation):
        return JSONResponse({"ok": False, "error": "Janela Meta encerrada. Envie um template oficial aprovado."}, status_code=400)
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
    if conversation.source != "TELEGRAM" and needs_pacing(db, conversation):
        body = (
            clean_caption or "[Imagem]" if kind == "IMAGE" else
            clean_caption or "[Video]" if kind == "VIDEO" else
            "[Audio]" if kind == "AUDIO" else safe_name
        )
        try:
            _claim_on_human_reply(db, conversation, user)
            queued, _slot = queue_manual_message(
                db, conversation, message_type=kind, body=body,
                sender_user_id=user.id,
                media_url=storage_uri, mime_type=mime_type,
                caption=clean_caption, filename=safe_name,
            )
            db.commit()
        except (ZApiError, ValueError) as exc:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "message_id": queued.id, "type": kind, "status": "PENDING"})
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

    _claim_on_human_reply(db, conversation, user)
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
    if not message or not _message_visible_in_conversation(db, user, message):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not message.media_url:
        # Old Meta messages kept the media ID in the callback, but not the file.
        if not archive_meta_image(db, message):
            raise HTTPException(status_code=404, detail="Imagem indisponível ou expirada na Meta.")
        db.commit()
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
    if protected_conversation(db, conversation.id):
        raise HTTPException(status_code=403, detail="Instancia de atendimento exclusivo.")
    eligible = {item.id: item for item in _eligible_users(db, user, conversation)}
    target = eligible.get(assigned_user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    # Reatribuir a uma pessoa encerra imediatamente a delegacao da SulIA.
    # Sem revogar MANUAL_AI, a IA poderia voltar antes de o humano responder.
    queue_link = db.scalar(
        select(ConversationQueueLink).where(
            ConversationQueueLink.conversation_id == conversation.id
        )
    )
    if queue_link and queue_link.origin == MANUAL_AI_ORIGIN:
        queue_link.origin = MANUAL_TRANSFER_ORIGIN
        from app.models import AIConversationState  # noqa: PLC0415

        ai_state = db.scalar(
            select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id
            )
        )
        if ai_state and ai_state.state in {"AI_ACTIVE", "CLOSING"}:
            ai_state.state = "HUMAN_ACTIVE"
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
    if protected_conversation(db, conversation.id):
        raise HTTPException(status_code=403, detail="Instancia de atendimento exclusivo.")
    current = queue_for_conversation(db, conversation.id)
    allowed = {item["id"] for item in _transfer_queues(db, user, current)}
    queue = get_queue(db, queue_id)
    if not queue or not queue.active or queue.id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    # A fila Digital do WhatsApp oficial não é intercambiável com filas Z-API.
    if _has_meta_link(db, conversation) or queue_meta_account(db, queue.id):
        raise HTTPException(status_code=409, detail="Transfira o responsável dentro da fila Meta, sem trocar o canal.")

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


@router.post("/conversations/{conversation_id}/takeover-human")
def takeover_conversation_human(
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
    if not has_permission(user, "conversations.transfer") or not has_permission(user, "conversations.reply"):
        raise HTTPException(status_code=403)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404)
    if protected_conversation(db, conversation.id) and conversation.assigned_user_id != user.id:
        raise HTTPException(status_code=403, detail="Instancia de atendimento exclusivo.")
    if conversation.status in CLOSED_CONVERSATION_STATUSES:
        flash(request, "Este atendimento está encerrado.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    if db.scalar(select(AIAgentProfile.id).where(AIAgentProfile.user_id == user.id).limit(1)):
        raise HTTPException(status_code=403, detail="Somente um operador humano pode assumir.")

    previous = conversation.assigned_user_id
    # Registro persistente por conversa: não depende de horário ou da fila.
    db.add(ConversationAssignment(
        conversation_id=conversation.id, user_id=user.id,
        assigned_by_id=user.id, event=HUMAN_TAKEOVER_EVENT,
    ))
    conversation.assigned_user_id = user.id
    conversation.status = "EM_ATENDIMENTO"
    if conversation.lead:
        conversation.lead.assigned_user_id = user.id
    link = db.scalar(select(ConversationQueueLink).where(
        ConversationQueueLink.conversation_id == conversation.id
    ))
    if link and link.origin == MANUAL_AI_ORIGIN:
        link.origin = MANUAL_TRANSFER_ORIGIN
    state = db.scalar(select(AIConversationState).where(
        AIConversationState.conversation_id == conversation.id
    ))
    if state and state.state not in {"OPT_OUT", "COMPLETED"}:
        state.state = "HUMAN_ACTIVE"
        state.handoff_at = utcnow()

    jobs = list(db.scalars(select(AIJob).where(
        AIJob.conversation_id == conversation.id,
        AIJob.status.in_(("PENDENTE", "PROCESSANDO")),
    )))
    for job in jobs:
        job.status = "CANCELADO"
        job.locked_at = None
        job.error = "Atendimento assumido por operador humano."
    followups = cancel_pending_followups(db, conversation.id, reason="HUMAN_TAKEOVER")
    # Não alterar entregas já em trânsito ou confirmadas pelo provedor.
    pending_replies = list(db.scalars(
        select(Message)
        .join(AIReplyDelivery, AIReplyDelivery.outbound_message_id == Message.id)
        .where(
            Message.conversation_id == conversation.id,
            Message.status.in_(("PENDING", "FAILED", "ERROR")),
            Message.provider_message_id.is_(None),
            Message.provider_zaap_id.is_(None),
            Message.sent_at.is_(None),
            Message.delivered_at.is_(None),
        )
    ))
    for reply in pending_replies:
        reply.status = "CANCELED"
    write_audit(
        db, user=user, action="CONVERSATION_HUMAN_TAKEOVER",
        entity_type="conversation", entity_id=conversation.id,
        payload={"from_user_id": previous, "to_user_id": user.id,
                 "jobs_cancelled": len(jobs), "followups_cancelled": followups,
                 "replies_cancelled": len(pending_replies)},
    )
    db.commit()
    flash(request, "Você assumiu o atendimento. A IA está pausada somente nesta conversa.")
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


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
    ai_queue = ai_transfer_queue(db)
    if not ai_queue:
        flash(request, "A SulIA precisa estar ativa e autorizada para receber o atendimento.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    # A transferencia e de responsabilidade, NAO de fila. A saida continua
    # no mesmo WhatsApp usado para falar com o cliente.
    queue = queue_for_conversation(db, conversation.id)
    if not queue:
        flash(request, "A conversa precisa estar vinculada a uma fila de origem.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)

    if human_takeover_locked(db, conversation.id):
        db.add(ConversationAssignment(
            conversation_id=conversation.id, user_id=None,
            assigned_by_id=user.id, event=AI_RESUMED_EVENT,
        ))
        db.flush()
        write_audit(db, user=user, action="CONVERSATION_AI_RESUMED",
                    entity_type="conversation", entity_id=conversation.id)
    result = _move_conversation_to_queue(
        db,
        actor=user,
        conversation=conversation,
        queue=queue,
        reason="AI",
    )
    db.commit()
    if result["ai_job"]:
        flash(request, f"Conversa delegada à SulIA na fila {queue.name}, usando o WhatsApp original. Ela vai responder o cliente agora.")
    elif result["pending_message"]:
        flash(
            request,
            f"Conversa mantida na fila {queue.name}, mas a IA não pôde assumir "
            "(cliente pediu para sair ou atendimento já concluído). Atribua a um vendedor.",
            "warning",
        )
    else:
        flash(
            request,
            f"Conversa delegada à SulIA na fila {queue.name}, com WhatsApp original. Não há mensagem do cliente pendente; "
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
    queue_labels = _conversation_queue_labels(db, ids)
    ai_reply_statuses = reply_statuses(
        db, [cid for cid, label in queue_labels.items() if label["code"] == "FILA-IA"],
    )
    tags = tag_map(db, ids)
    insights = insight_map(db, ids)
    privacy_cutoffs = _inbox_privacy_cutoffs(db, user, conversations)
    for private_id in privacy_cutoffs:
        tags.pop(private_id, None)
        insights.pop(private_id, None)
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
                "preview": _private_preview(
                    item, None if _may_read_full_conversation_history(user, item)
                    else privacy_cutoffs.get(item.id),
                ),
                "queue_id": queue_labels.get(item.id, {}).get("id"),
                "queue_name": queue_labels.get(item.id, {}).get("name"),
                "ai_reply_status": ai_reply_statuses.get(item.id),
                "owner": item.assigned_user.name if item.assigned_user else "Sem responsável",
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
    stmt = select(Message).where(
        Message.conversation_id == conversation.id,
        Message.id > max(after_id, 0),
    )
    since = _history_since(db, user, conversation)
    if since is not None:
        stmt = stmt.where(Message.created_at >= since)
    stmt = stmt.options(selectinload(Message.sender_user)).order_by(Message.id.asc())
    items = list(db.scalars(stmt))
    if conversation.unread_count:
        mark_conversation_read(db, conversation)
        db.commit()
    location_urls = location_urls_for(db, items)
    recoverable_images = meta_image_ids(db, items)
    selected_queue = queue_for_conversation(db, conversation.id)
    ai_reply_status = (
        reply_statuses(db, [conversation.id]).get(conversation.id)
        if is_dedicated_ai_queue(selected_queue)
        else None
    )
    return {
        "conversation_id": conversation.id,
        "ai_reply_status": ai_reply_status,
        "items": [
            {
                "id": item.id,
                "map_url": location_urls.get(item.id),
                "direction": item.direction,
                "type": item.message_type,
                "body": item.body,
                "media_url": f"/conversations/messages/{item.id}/media" if item.media_url or item.id in recoverable_images else None,
                "mime_type": item.mime_type,
                "status": item.status,
                "sender": item.sender_user.name if item.sender_user else ("Cliente" if item.direction == "INBOUND" else "Sulnet"),
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ],
    }



@router.get("/conversations/{conversation_id}/crm-origins")
def crm_registration_origins(
    conversation_id: int, request: Request, db: Session = Depends(get_db),
):
    headers = {"Cache-Control": "no-store"}
    user, redirect = _user_or_redirect(request, db)
    if redirect or not has_permission(user, "crm.register"):
        return JSONResponse({"error": "Acesso não autorizado."}, status_code=403, headers=headers)
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return JSONResponse({"error": "Conversa indisponível."}, status_code=404, headers=headers)
    seller = seller_for_user(conversation.assigned_user)
    if seller is None or (user.role == "CONSULTOR" and conversation.assigned_user_id != user.id):
        return JSONResponse({"error": "Vendedor não autorizado."}, status_code=403, headers=headers)
    try:
        origins = SulnetCRMClient.from_settings().list_active_origins()
    except CRMIntegrationError:
        return JSONResponse(
            {"error": "Não foi possível carregar as origens do CRM. Tente novamente."},
            status_code=503, headers=headers,
        )
    suggested, _ = resolve_conversation_origin(db, conversation.id)
    default = next((row["id"] for row in origins if row["name"] == suggested), None)
    if default is None:
        default = next((row["id"] for row in origins if row["name"] == "Sulnet Prospecta"), None)
    return JSONResponse({"origins": origins, "defaultId": default}, headers=headers)


@router.post("/conversations/{conversation_id}/register-crm")
def register_crm_manually(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    client_name: str = Form(...),
    origin_id: str = Form(""),
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
        ai_owner = bool(conversation.assigned_user_id and db.scalar(
            select(AIAgentProfile.id).where(
                AIAgentProfile.user_id == conversation.assigned_user_id,
                AIAgentProfile.active.is_(True),
            ).limit(1)
        ))
        if ai_owner:
            if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
                raise CRMManualError("A liberação manual da IA é exclusiva da supervisão.")
            crm_prospect_sync.sync_prospects(
                db, conversation_id=conversation.id, limit=1, force=True,
            )
            opportunity_id = crm_prospect_sync.ai_crm_opportunity_id(db, conversation.id)
            if not opportunity_id:
                raise CRMManualError(
                    "O cadastro automático da IA ainda não foi confirmado pelo CRM. "
                    "Confira a integração antes de repetir."
                )
            result = {
                "already": True,
                "opportunityId": opportunity_id,
                "protocol": "",
            }
        else:
            result = register_conversation(
                db,
                conversation=conversation,
                actor=user,
                client_name=client_name,
                origin_id=origin_id,
            )
    except (CRMManualError, CRMIntegrationError) as exc:
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


@router.post("/conversations/{conversation_id}/consult-cpf")
def consult_cpf_manually(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    cpf: str = Form(...),
    db: Session = Depends(get_db),
):
    """Consulta comercial vinculada a lead já registrado; não inicia IA."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return _conversation_unavailable(request)
    try:
        result = consult_registered_cpf(
            db, conversation=conversation, actor=user, cpf=cpf,
        )
    except CRMManualError as exc:
        db.rollback()
        flash(request, str(exc), "warning")
    else:
        flash(
            request,
            (
                "Consulta já registrada no CRM: "
                if result.get("already") else "Consulta de crédito concluída no CRM: "
            ) + str(result.get("statusLabel") or "Em análise"),
            "success",
        )
    return RedirectResponse(f"/conversations/{conversation_id}", status_code=303)


@router.get("/conversations/{conversation_id}/refresh-credit-status")
def refresh_credit_status(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Atualiza o resultado local com credit-status do CRM (sem nova Serasa)."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return JSONResponse({"ok": False, "message": "Conversa indisponível."}, status_code=404)
    try:
        result = refresh_credit_status_from_crm(
            db, conversation=conversation, actor=user,
        )
    except CRMManualError as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"ok": True, **result},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/conversations/{conversation_id}/credit-contestation")
def get_credit_contestation_info(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Leitura pontual de motivos e elegibilidade na análise já existente.

    Nunca dispara Serasa, não expõe CPF, não usa dados antigos para autorizar.
    """
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return JSONResponse({"ok": False, "message": "Conversa indisponível."}, status_code=404)
    try:
        result = get_contestation_panel(db, conversation=conversation, actor=user)
    except CRMManualError as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"ok": True, **result},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/conversations/{conversation_id}/credit-contestation")
def submit_credit_contestation(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    reason_type: str = Form(...),
    justification: str = Form(""),
    doc_confirmed: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Contestação explícita do vendedor; anexa documentos somente no CRM."""
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if conversation is None:
        return _conversation_unavailable(request)
    try:
        if len(files) > 3:
            raise CRMManualError("É permitido anexar no máximo três documentos.")
        documents: list[tuple[str, str, bytes]] = []
        for uploaded in files:
            if not uploaded.filename:
                continue
            blob = uploaded.file.read(CONTEST_MAX_FILE_SIZE + 1)
            if len(blob) > CONTEST_MAX_FILE_SIZE:
                raise CRMManualError("Cada documento pode ter no máximo 10 MB.")
            documents.append((
                uploaded.filename,
                uploaded.content_type or "application/octet-stream",
                blob,
            ))
        send_contestation(
            db,
            conversation=conversation,
            actor=user,
            reason_type=reason_type,
            justification=justification,
            doc_confirmed=doc_confirmed,
            documents=documents,
        )
    except CRMManualError as exc:
        db.rollback()
        flash(request, str(exc), "warning")
    else:
        flash(request, "Contestação enviada ao CRM Comercial. Aguardando decisão gerencial.", "success")
    finally:
        for uploaded in files:
            uploaded.file.close()
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
