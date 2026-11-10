from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIQueueBinding,
    Contact,
    Conversation,
    ConversationAssignment,
    ConversationQueueLink,
    ConversationZApiInstance,
    Lead,
    Message,
    MessageEvent,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.services.queue_routing import (
    AI_OWNED_STATES,
    AI_WHEN_HUMAN_IDLE,
    AI_WHEN_OFFLINE,
    ai_owns_conversation,
    assign_queue_user,
    bind_conversation_queue,
    human_online_in_queue,
    queue_for_conversation,
    resolve_queue,
)
from app.services.scoring import normalize_phone
from app.services.zapi_instances import bind_conversation_instance
from app.services.whatsapp_presence import mark_whatsapp_assignment, online_user_ids, presence_for_users
from app.timeutils import utcnow


OPEN_CONVERSATION_STATUSES = {
    "NOVO",
    "EM_FILA",
    "ATRIBUIDO",
    "EM_ATENDIMENTO",
    "AGUARDANDO_CLIENTE",
    "AGUARDANDO_INTERNO",
    "FOLLOW_UP",
}
CLOSED_CONVERSATION_STATUSES = {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}


def find_lead_by_phone(db: Session, phone: str | None) -> Lead | None:
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    lead = db.scalar(select(Lead).where(Lead.phone == normalized))
    if lead:
        return lead
    suffix = normalized[-10:]
    return db.scalar(select(Lead).where(Lead.phone.endswith(suffix)).limit(1))


def ensure_contact(
    db: Session,
    phone: str | None,
    *,
    lead: Lead | None = None,
    name: str | None = None,
    business_name: str | None = None,
    email: str | None = None,
) -> Contact:
    normalized = normalize_phone(phone)
    if not normalized:
        raise ValueError("Telefone invalido para criar contato de conversa.")
    contact = db.scalar(select(Contact).where(Contact.phone == normalized))
    if not contact:
        contact = Contact(
            phone=normalized,
            lead_id=lead.id if lead else None,
            name=(name or (lead.contact_name if lead else "") or "").strip(),
            business_name=(business_name or (lead.business_name if lead else "") or "").strip(),
            email=(email or (lead.email if lead else None)),
        )
        db.add(contact)
        db.flush()
    else:
        if lead and not contact.lead_id:
            contact.lead_id = lead.id
        if name and not contact.name:
            contact.name = name.strip()
        if business_name and not contact.business_name:
            contact.business_name = business_name.strip()
        if email and not contact.email:
            contact.email = email
    contact.last_seen_at = utcnow()
    return contact


def _sticky_owner(
    db: Session,
    contact: Contact,
    *,
    online_ids: set[int] | None = None,
) -> User | None:
    previous = db.scalar(
        select(Conversation)
        .where(
            Conversation.contact_id == contact.id,
            Conversation.assigned_user_id.is_not(None),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )
    if not previous or not previous.assigned_user_id:
        return None
    if online_ids is not None and previous.assigned_user_id not in online_ids:
        return None
    user = db.get(User, previous.assigned_user_id)
    return user if user and user.active else None


def _presence_timestamp(value) -> float:
    if not value:
        return 0.0
    if value.tzinfo is None:
        from datetime import timezone
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _queue_reserved_for_ai(
    db: Session,
    queue: ServiceQueue | None,
    conversation_id: int | None = None,
) -> bool:
    if not queue or not settings.ai_sdr_enabled or not settings.ai_autonomous_enabled:
        return False
    binding = db.scalar(
        select(AIQueueBinding).where(
            AIQueueBinding.queue_id == queue.id,
            AIQueueBinding.active.is_(True),
            AIQueueBinding.handling_mode.in_(
                {"AI_FIRST", "AI_ONLY", AI_WHEN_OFFLINE, AI_WHEN_HUMAN_IDLE}
            ),
        )
    )
    if binding is None:
        return False
    if binding.handling_mode == AI_WHEN_HUMAN_IDLE:
        # Fila comercial: com vendedor online a conversa fica com ele; a IA só
        # assume se já era dela ou após 10 min sem interação (worker).
        return ai_owns_conversation(db, conversation_id) or not human_online_in_queue(
            db, queue.id
        )
    # Fila da IA: só a IA trabalha, mesmo com vendedor online (06/11/2026).
    # "IA quando não houver vendedor online" passou a funcionar como IA sempre.
    return True


def _conversation_ready_for_human(db: Session, conversation_id: int) -> bool:
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    return bool(state and state.state in {"HANDOFF", "HUMAN_ACTIVE"})


def choose_assignee(
    db: Session,
    contact: Contact,
    lead: Lead | None,
    *,
    queue: ServiceQueue | None = None,
) -> User | None:
    if queue:
        return assign_queue_user(db, queue)

    online_ids = set(online_user_ids(db))
    if not online_ids:
        return None

    if lead and lead.assigned_user_id and lead.assigned_user_id in online_ids:
        assigned = db.get(User, lead.assigned_user_id)
        if assigned and assigned.active:
            return assigned

    sticky = _sticky_owner(db, contact, online_ids=online_ids)
    if sticky:
        return sticky

    target_team = lead.team if lead and lead.team else "B2B"
    stmt = select(User).where(
        User.active.is_(True),
        User.id.in_(online_ids),
        User.team == target_team,
    )
    candidates = list(db.scalars(stmt.order_by(User.id)))
    if lead and lead.branch_id:
        branch_candidates = [u for u in candidates if u.branch_id == lead.branch_id]
        if branch_candidates:
            candidates = branch_candidates
    if not candidates:
        return None

    presences = presence_for_users(db, [item.id for item in candidates])
    ranked: list[tuple[float, int, int, User]] = []
    for candidate in candidates:
        open_count = db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.assigned_user_id == candidate.id,
                Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            )
        ) or 0
        presence = presences.get(candidate.id)
        last_assigned = _presence_timestamp(
            presence.last_assigned_at if presence else None
        )
        ranked.append(
            (
                last_assigned,
                int(open_count),
                candidate.id,
                candidate,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3]


def ensure_open_conversation(
    db: Session,
    contact: Contact,
    *,
    lead: Lead | None = None,
    source: str = "WHATSAPP",
    queue_id: int | None = None,
    queue_origin: str = "INBOUND",
    assign_if_unowned: bool = True,
    zapi_instance_id: int | None = None,
) -> Conversation:
    if zapi_instance_id:
        conversation = db.scalar(
            select(Conversation)
            .join(
                ConversationZApiInstance,
                ConversationZApiInstance.conversation_id == Conversation.id,
            )
            .where(
                Conversation.contact_id == contact.id,
                Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
                ConversationZApiInstance.zapi_instance_id == zapi_instance_id,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        if not conversation:
            conversation = db.scalar(
                select(Conversation)
                .outerjoin(
                    ConversationZApiInstance,
                    ConversationZApiInstance.conversation_id == Conversation.id,
                )
                .where(
                    Conversation.contact_id == contact.id,
                    Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
                    ConversationZApiInstance.id.is_(None),
                )
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
    else:
        conversation = db.scalar(
            select(Conversation)
            .where(
                Conversation.contact_id == contact.id,
                Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            )
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
    team = lead.team if lead and lead.team else "B2B"
    branch_id = lead.branch_id if lead else None

    if conversation:
        if zapi_instance_id:
            bind_conversation_instance(db, conversation.id, zapi_instance_id)
        if lead and not conversation.lead_id:
            conversation.lead_id = lead.id
            conversation.branch_id = lead.branch_id
            conversation.team = lead.team

        existing_queue = queue_for_conversation(db, conversation.id)
        # Quando a mensagem entra por uma instância Z-API vinculada a uma fila,
        # essa fila é a fonte oficial do roteamento de entrada. Isso corrige
        # conversas antigas que ainda estavam presas a outra fila.
        explicit_inbound_queue = queue_id if zapi_instance_id and queue_id else None
        queue = resolve_queue(
            db,
            conversation_id=conversation.id,
            explicit_queue_id=(
                explicit_inbound_queue
                if explicit_inbound_queue
                else (queue_id if not existing_queue else None)
            ),
            lead_id=lead.id if lead else conversation.lead_id,
            team=conversation.team or team,
            branch_id=conversation.branch_id or branch_id,
        )
        queue_changed = bool(
            queue
            and existing_queue
            and queue.id != existing_queue.id
        )
        if queue:
            bind_conversation_queue(
                db,
                conversation.id,
                queue.id,
                origin=queue_origin,
            )
            conversation.team = queue.team
            if queue.branch_id and not conversation.branch_id:
                conversation.branch_id = queue.branch_id

            # Se a conversa foi realocada pela instância para uma fila reservada
            # à IA, retire uma atribuição humana antiga. Handoffs já realizados
            # permanecem humanos.
            if (
                queue_changed
                and _queue_reserved_for_ai(db, queue, conversation.id)
                and not _conversation_ready_for_human(db, conversation.id)
                and conversation.assigned_user_id
            ):
                conversation.assigned_user_id = None
                conversation.status = "EM_FILA"
                db.add(
                    ConversationAssignment(
                        conversation_id=conversation.id,
                        user_id=None,
                        assigned_by_id=None,
                        event="AI_QUEUE_REBOUND",
                    )
                )

        if (
            assign_if_unowned
            and not conversation.assigned_user_id
            and (
                not _queue_reserved_for_ai(db, queue, conversation.id)
                or _conversation_ready_for_human(db, conversation.id)
            )
        ):
            owner = choose_assignee(db, contact, lead, queue=queue)
            if owner:
                conversation.assigned_user_id = owner.id
                conversation.status = "ATRIBUIDO"
                db.add(
                    ConversationAssignment(
                        conversation_id=conversation.id,
                        user_id=owner.id,
                        assigned_by_id=None,
                        event="AUTO_ASSIGNED_QUEUE" if queue else "AUTO_ASSIGNED",
                    )
                )
                if queue is None:
                    mark_whatsapp_assignment(db, owner.id)
                if lead and lead.assigned_user_id != owner.id:
                    lead.assigned_user_id = owner.id
        return conversation

    queue = resolve_queue(
        db,
        explicit_queue_id=queue_id,
        lead_id=lead.id if lead else contact.lead_id,
        team=team,
        branch_id=branch_id,
    )
    ai_reserved = _queue_reserved_for_ai(db, queue)
    owner = (
        choose_assignee(db, contact, lead, queue=queue)
        if assign_if_unowned and not ai_reserved
        else None
    )
    conversation = Conversation(
        contact_id=contact.id,
        lead_id=lead.id if lead else contact.lead_id,
        assigned_user_id=owner.id if owner else None,
        branch_id=(
            lead.branch_id
            if lead and lead.branch_id
            else (queue.branch_id if queue else None)
        ),
        team=queue.team if queue else team,
        source=source,
        status="ATRIBUIDO" if owner else "EM_FILA",
        opened_at=utcnow(),
    )
    db.add(conversation)
    db.flush()

    if zapi_instance_id:
        bind_conversation_instance(db, conversation.id, zapi_instance_id)

    if queue:
        bind_conversation_queue(
            db,
            conversation.id,
            queue.id,
            origin=queue_origin,
        )

    if owner and lead and lead.assigned_user_id != owner.id:
        lead.assigned_user_id = owner.id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=owner.id if owner else None,
            assigned_by_id=None,
            event=(
                "AUTO_ASSIGNED_QUEUE"
                if owner and queue
                else "AUTO_ASSIGNED"
                if owner
                else "QUEUED"
            ),
        )
    )
    if owner and queue is None:
        mark_whatsapp_assignment(db, owner.id)
    return conversation


def distribute_waiting_conversations(
    db: Session,
    *,
    limit: int = 100,
) -> int:
    queued = list(
        db.scalars(
            select(Conversation)
            .where(
                Conversation.assigned_user_id.is_(None),
                Conversation.status.in_(
                    OPEN_CONVERSATION_STATUSES - {"AGUARDANDO_CLIENTE"}
                ),
            )
            .order_by(
                Conversation.opened_at.asc(),
                Conversation.id.asc(),
            )
            .limit(max(1, min(limit, 500)))
        )
    )
    assigned_count = 0
    for conversation in queued:
        contact = db.get(Contact, conversation.contact_id)
        if not contact:
            continue
        lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
        queue = queue_for_conversation(db, conversation.id)
        if not queue:
            queue = resolve_queue(
                db,
                lead_id=lead.id if lead else None,
                team=conversation.team or (lead.team if lead else "B2B"),
                branch_id=conversation.branch_id or (lead.branch_id if lead else None),
            )
            if queue:
                bind_conversation_queue(
                    db,
                    conversation.id,
                    queue.id,
                    origin="AUTO_QUEUE",
                )
        if _queue_reserved_for_ai(db, queue, conversation.id) and not _conversation_ready_for_human(
            db, conversation.id
        ):
            continue
        owner = choose_assignee(db, contact, lead, queue=queue)
        if not owner:
            continue
        conversation.assigned_user_id = owner.id
        if conversation.status in {"NOVO", "EM_FILA"}:
            conversation.status = "ATRIBUIDO"
        if lead and lead.assigned_user_id != owner.id:
            lead.assigned_user_id = owner.id
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=owner.id,
                assigned_by_id=None,
                event="AUTO_ASSIGNED_QUEUE" if queue else "AUTO_ASSIGNED_QUEUE",
            )
        )
        if queue is None:
            mark_whatsapp_assignment(db, owner.id)
        assigned_count += 1
    db.flush()
    return assigned_count


HUMAN_REQUESTED_AI_STATES = {"HANDOFF", "HUMAN_ACTIVE"}


def _ai_reserved_queue_ids():
    return select(AIQueueBinding.queue_id).where(
        AIQueueBinding.active.is_(True),
        AIQueueBinding.handling_mode.in_({"AI_FIRST", "AI_ONLY"}),
    )


def _ai_owned_offline_queue_conversation_ids():
    """Conversas que a IA já começou numa fila "IA quando não houver vendedor online"."""
    offline_queue_ids = select(AIQueueBinding.queue_id).where(
        AIQueueBinding.active.is_(True),
        AIQueueBinding.handling_mode.in_({AI_WHEN_OFFLINE, AI_WHEN_HUMAN_IDLE}),
    )
    return (
        select(ConversationQueueLink.conversation_id)
        .join(
            AIConversationState,
            AIConversationState.conversation_id == ConversationQueueLink.conversation_id,
        )
        .where(
            ConversationQueueLink.queue_id.in_(offline_queue_ids),
            AIConversationState.state.in_(AI_OWNED_STATES),
        )
    )


def _consultant_manual_queue_waiting(user: User):
    """Conversas sem dono que o vendedor pode pegar numa fila MANUAL.

    Disparos e campanhas nunca entram aqui: só o que pediu atendimento humano
    (handoff da IA) ou recebeu resposta do cliente numa fila sem IA.
    """
    manual_queue_ids = (
        select(ServiceQueueMember.queue_id)
        .join(ServiceQueue, ServiceQueue.id == ServiceQueueMember.queue_id)
        .where(
            ServiceQueueMember.user_id == user.id,
            ServiceQueueMember.active.is_(True),
            ServiceQueue.active.is_(True),
            ServiceQueue.routing_strategy == "MANUAL",
        )
    )
    manual_conversation_ids = select(ConversationQueueLink.conversation_id).where(
        ConversationQueueLink.queue_id.in_(manual_queue_ids)
    )
    human_requested_ids = select(AIConversationState.conversation_id).where(
        AIConversationState.state.in_(HUMAN_REQUESTED_AI_STATES)
    )
    waiting_for_human = Conversation.id.in_(human_requested_ids)
    if settings.ai_sdr_enabled and settings.ai_autonomous_enabled:
        human_queue_conversation_ids = select(ConversationQueueLink.conversation_id).where(
            ConversationQueueLink.queue_id.in_(manual_queue_ids),
            ConversationQueueLink.queue_id.not_in(_ai_reserved_queue_ids()),
            ConversationQueueLink.conversation_id.not_in(_ai_owned_offline_queue_conversation_ids()),
        )
    else:
        human_queue_conversation_ids = manual_conversation_ids
    customer_replied_in_human_queue = (Conversation.unread_count > 0) & Conversation.id.in_(
        human_queue_conversation_ids
    )
    return (
        Conversation.assigned_user_id.is_(None)
        & Conversation.status.in_(OPEN_CONVERSATION_STATUSES)
        & Conversation.id.in_(manual_conversation_ids)
        & or_(waiting_for_human, customer_replied_in_human_queue)
    )


def apply_conversation_scope(stmt, user: User, *, inbox: bool = False):
    """Restringe as conversas que cada perfil enxerga.

    - ADMIN/GESTOR: tudo, inclusive disparos e conversas com a IA.
    - SUPERVISOR: a própria filial/equipe; na caixa de entrada (inbox=True),
      somente o que já está com algum vendedor.
    - CONSULTOR: somente as conversas atribuídas a ele. A exceção são filas
      MANUAL, em que ele vê o que pediu atendimento humano para poder pegar.
    """
    if user.role in {"ADMIN", "GESTOR"}:
        return stmt
    if user.role == "SUPERVISOR":
        if user.branch_id:
            stmt = stmt.where(Conversation.branch_id == user.branch_id)
        else:
            stmt = stmt.where(Conversation.team == user.team)
        if inbox:
            stmt = stmt.where(Conversation.assigned_user_id.is_not(None))
            # Conversa da IA não aparece para vendedores/supervisores na caixa.
            stmt = stmt.where(
                Conversation.assigned_user_id.not_in(
                    select(AIAgentProfile.user_id).where(AIAgentProfile.user_id.is_not(None))
                )
            )
        return stmt
    if user.role == "CONSULTOR":
        return stmt.where(
            or_(
                Conversation.assigned_user_id == user.id,
                _consultant_manual_queue_waiting(user),
            )
        )
    return stmt.where(Conversation.assigned_user_id == user.id)


def conversation_visible_to_user(conversation: Conversation, user: User) -> bool:
    if user.role in {"ADMIN", "GESTOR"}:
        return True
    if user.role == "SUPERVISOR":
        if conversation.assigned_user_id is None:
            return False
        if user.branch_id:
            return conversation.branch_id == user.branch_id
        return conversation.team == user.team
    return conversation.assigned_user_id == user.id


def payload_message_content(payload: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    text = payload.get("text")
    if isinstance(text, dict) and text.get("message"):
        return "TEXT", str(text["message"]).strip(), None, None

    buttons_response = payload.get("buttonsResponseMessage")
    if isinstance(buttons_response, dict):
        body = (
            buttons_response.get("message")
            or buttons_response.get("selectedDisplayText")
            or buttons_response.get("selectedButtonId")
            or buttons_response.get("buttonId")
            or "[Resposta de botão]"
        )
        return "TEXT", str(body).strip(), None, None

    list_response = payload.get("listResponseMessage")
    if isinstance(list_response, dict):
        body = (
            list_response.get("message")
            or list_response.get("title")
            or list_response.get("selectedRowId")
            or list_response.get("description")
            or "[Resposta de lista]"
        )
        return "TEXT", str(body).strip(), None, None

    image = payload.get("image")
    if isinstance(image, dict):
        return (
            "IMAGE",
            str(image.get("caption") or "[Imagem]").strip(),
            image.get("imageUrl"),
            image.get("mimeType"),
        )

    audio = payload.get("audio")
    if isinstance(audio, dict):
        return "AUDIO", "[Audio]", audio.get("audioUrl"), audio.get("mimeType")

    video = payload.get("video")
    if isinstance(video, dict):
        return (
            "VIDEO",
            str(video.get("caption") or "[Video]").strip(),
            video.get("videoUrl"),
            video.get("mimeType"),
        )

    document = payload.get("document")
    if isinstance(document, dict):
        label = document.get("fileName") or document.get("title") or "[Documento]"
        return "DOCUMENT", str(label).strip(), document.get("documentUrl"), document.get("mimeType")

    sticker = payload.get("sticker")
    if isinstance(sticker, dict):
        return "STICKER", "[Sticker]", sticker.get("stickerUrl"), sticker.get("mimeType")

    location = payload.get("location")
    if isinstance(location, dict):
        body = location.get("name") or location.get("address") or "[Localizacao]"
        return "LOCATION", str(body).strip(), location.get("url"), None

    contact = payload.get("contact")
    if isinstance(contact, dict):
        return "CONTACT", str(contact.get("displayName") or "[Contato]").strip(), None, None

    reaction = payload.get("reaction")
    if isinstance(reaction, dict):
        return "REACTION", str(reaction.get("value") or "[Reacao]"), None, None

    return "UNKNOWN", "", None, None


def _preview(body: str, message_type: str) -> str:
    value = (body or f"[{message_type}]").replace("\n", " ").strip()
    return value[:240]


def register_message(
    db: Session,
    conversation: Conversation,
    *,
    direction: str,
    message_type: str,
    body: str,
    sender_user_id: int | None = None,
    media_url: str | None = None,
    mime_type: str | None = None,
    provider_message_id: str | None = None,
    provider_zaap_id: str | None = None,
    status: str = "PENDING",
    increment_unread: bool = False,
) -> Message:
    now = utcnow()
    message = Message(
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        sender_user_id=sender_user_id,
        direction=direction,
        message_type=message_type,
        body=body,
        media_url=media_url,
        mime_type=mime_type,
        provider_message_id=provider_message_id,
        provider_zaap_id=provider_zaap_id,
        status=status,
        sent_at=now if direction == "OUTBOUND" else None,
    )
    db.add(message)
    conversation.last_message_at = now
    conversation.last_message_preview = _preview(body, message_type)
    if increment_unread:
        conversation.unread_count = int(conversation.unread_count or 0) + 1
    if direction == "INBOUND":
        if not conversation.assigned_user_id:
            conversation.status = "EM_FILA"
        elif conversation.status in {"NOVO", "EM_FILA", "ATRIBUIDO", "AGUARDANDO_CLIENTE"}:
            conversation.status = "EM_ATENDIMENTO"
    elif direction == "OUTBOUND" and conversation.status not in CLOSED_CONVERSATION_STATUSES:
        conversation.status = "AGUARDANDO_CLIENTE"
    return message


def payload_from_me(payload: dict[str, Any]) -> bool:
    value = payload.get("fromMe")
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "sim", "on"}
    return False


def store_received_payload(
    db: Session,
    payload: dict[str, Any],
    *,
    queue_id: int | None = None,
    zapi_instance_id: int | None = None,
) -> tuple[Conversation | None, Message | None, bool]:
    if payload.get("isGroup") or payload.get("isNewsletter") or payload.get("isStatusReply"):
        return None, None, False
    phone = payload.get("phone") or payload.get("participantPhone")
    normalized = normalize_phone(phone)
    if not normalized:
        return None, None, False

    provider_id = str(payload.get("messageId") or payload.get("id") or "").strip() or None
    if provider_id:
        existing = db.scalar(select(Message).where(Message.provider_message_id == provider_id))
        if existing:
            if payload.get("status"):
                existing.status = str(payload["status"]).upper()
            return existing.conversation, existing, False

    message_type, body, media_url, mime_type = payload_message_content(payload)
    if not body and message_type == "UNKNOWN":
        return None, None, False

    lead = find_lead_by_phone(db, normalized)
    sender_name = str(payload.get("senderName") or payload.get("chatName") or "").strip()
    contact = ensure_contact(
        db,
        normalized,
        lead=lead,
        name=sender_name if not lead else None,
        business_name=lead.business_name if lead else sender_name,
    )
    conversation = ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source="WHATSAPP",
        queue_id=queue_id,
        queue_origin="ZAPI_INSTANCE" if zapi_instance_id else "INBOUND",
        zapi_instance_id=zapi_instance_id,
    )

    from_me = payload_from_me(payload)
    if message_type == "AUDIO" and not from_me:
        # Áudio do cliente vira texto para a IA conseguir entender e responder.
        from app.services.audio_transcription import transcribe_audio_url

        from app.services.audio_transcription import TRANSCRIBED_PREFIX, UNREADABLE_BODY

        transcript = transcribe_audio_url(media_url, mime_type)
        # Mesmo sem transcrição o áudio vira texto: a IA responde pedindo para
        # o cliente escrever, em vez de deixar a mensagem sem resposta.
        message_type = "TEXT"
        body = f"{TRANSCRIBED_PREFIX} {transcript}" if transcript else UNREADABLE_BODY
    direction = "OUTBOUND" if from_me else "INBOUND"
    message = register_message(
        db,
        conversation,
        direction=direction,
        message_type=message_type,
        body=body,
        media_url=media_url,
        mime_type=mime_type,
        provider_message_id=provider_id,
        status=str(payload.get("status") or ("SENT" if from_me else "RECEIVED")).upper(),
        increment_unread=not from_me,
    )
    db.flush()
    db.add(
        MessageEvent(
            message_id=message.id,
            provider_message_id=provider_id,
            event_type="RECEIVED_CALLBACK",
            status=message.status,
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        )
    )
    if not from_me and message.message_type not in {"TEXT", "BUTTONS", "AUDIO"}:
        # Anexo com o crédito reprovado é o comprovante do provedor: avisa o
        # consultor e responde o cliente (imagem não gera job de IA).
        try:
            from app.services.ai_seller import handle_credit_proof_attachment

            handle_credit_proof_attachment(db, conversation, message)
        except Exception:  # noqa: BLE001 - o webhook não pode falhar por causa do aviso
            import logging

            logging.getLogger("sulnet_conversation_engine").warning(
                "credit_proof_hook_failed conversation=%s", conversation.id
            )
    if not from_me:
        from app.services.ai_sdr import enqueue_ai_job
        enqueue_ai_job(
            db,
            conversation=conversation,
            message=message,
        )
    return conversation, message, True


def update_message_statuses(db: Session, payload: dict[str, Any]) -> int:
    status_value = str(payload.get("status") or "").upper().strip()
    ids = payload.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        single = payload.get("messageId") or payload.get("id")
        if single:
            ids = [single]
    ids = [str(item) for item in ids if item]
    if not ids:
        return 0

    now = utcnow()
    messages = list(
        db.scalars(
            select(Message).where(
                or_(
                    Message.provider_message_id.in_(ids),
                    Message.provider_zaap_id.in_(ids),
                )
            )
        )
    )
    for message in messages:
        if status_value:
            message.status = status_value
        if status_value == "RECEIVED":
            message.delivered_at = message.delivered_at or now
        if status_value in {"READ", "PLAYED"}:
            message.delivered_at = message.delivered_at or now
            message.read_at = message.read_at or now
        db.add(
            MessageEvent(
                message_id=message.id,
                provider_message_id=message.provider_message_id,
                event_type="STATUS",
                status=status_value or None,
                payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            )
        )
    if not messages:
        for item in ids:
            db.add(
                MessageEvent(
                    message_id=None,
                    provider_message_id=item,
                    event_type="STATUS_UNMATCHED",
                    status=status_value or None,
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                )
            )
    return len(messages)


def mark_conversation_read(db: Session, conversation: Conversation) -> None:
    conversation.unread_count = 0
    db.flush()
