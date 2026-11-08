from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AIAgentProfile,
    AIConversationState,
    CampaignQueueLink,
    Conversation,
    ConversationQueueLink,
    LeadQueuePreference,
    ProspectingQueueLink,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.services.whatsapp_presence import online_user_ids
from app.timeutils import utcnow


OPEN_STATUSES = {
    "NOVO",
    "EM_FILA",
    "ATRIBUIDO",
    "EM_ATENDIMENTO",
    "AGUARDANDO_CLIENTE",
    "AGUARDANDO_INTERNO",
    "FOLLOW_UP",
}


def _timestamp(value: datetime | None) -> float:
    if not value:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def get_queue(db: Session, queue_id: int | None) -> ServiceQueue | None:
    if not queue_id:
        return None
    queue = db.get(ServiceQueue, int(queue_id))
    return queue if queue and queue.active else None


def queue_for_conversation(db: Session, conversation_id: int) -> ServiceQueue | None:
    link = db.scalar(
        select(ConversationQueueLink)
        .options(selectinload(ConversationQueueLink.queue))
        .where(ConversationQueueLink.conversation_id == conversation_id)
    )
    if not link or not link.queue or not link.queue.active:
        return None
    return link.queue


def queue_for_campaign(db: Session, campaign_id: int) -> ServiceQueue | None:
    link = db.scalar(
        select(CampaignQueueLink)
        .options(selectinload(CampaignQueueLink.queue))
        .where(CampaignQueueLink.campaign_id == campaign_id)
    )
    if not link or not link.queue or not link.queue.active:
        return None
    return link.queue


def queue_for_prospecting_job(db: Session, job_id: int) -> ServiceQueue | None:
    link = db.scalar(
        select(ProspectingQueueLink)
        .options(selectinload(ProspectingQueueLink.queue))
        .where(ProspectingQueueLink.prospecting_job_id == job_id)
    )
    if not link or not link.queue or not link.queue.active:
        return None
    return link.queue


def preferred_queue_for_lead(db: Session, lead_id: int | None) -> ServiceQueue | None:
    if not lead_id:
        return None
    preference = db.scalar(
        select(LeadQueuePreference)
        .options(selectinload(LeadQueuePreference.queue))
        .where(LeadQueuePreference.lead_id == lead_id)
    )
    if not preference or not preference.queue or not preference.queue.active:
        return None
    return preference.queue


def set_lead_queue_preference(
    db: Session,
    lead_id: int,
    queue_id: int,
    *,
    source: str,
) -> LeadQueuePreference:
    preference = db.scalar(
        select(LeadQueuePreference).where(LeadQueuePreference.lead_id == lead_id)
    )
    if not preference:
        preference = LeadQueuePreference(
            lead_id=lead_id,
            queue_id=queue_id,
            source=source,
        )
        db.add(preference)
    else:
        preference.queue_id = queue_id
        preference.source = source
    db.flush()
    return preference


def bind_campaign_queue(db: Session, campaign_id: int, queue_id: int) -> CampaignQueueLink:
    link = db.scalar(
        select(CampaignQueueLink).where(CampaignQueueLink.campaign_id == campaign_id)
    )
    if not link:
        link = CampaignQueueLink(campaign_id=campaign_id, queue_id=queue_id)
        db.add(link)
    else:
        link.queue_id = queue_id
    db.flush()
    return link


def bind_prospecting_queue(db: Session, job_id: int, queue_id: int) -> ProspectingQueueLink:
    link = db.scalar(
        select(ProspectingQueueLink).where(ProspectingQueueLink.prospecting_job_id == job_id)
    )
    if not link:
        link = ProspectingQueueLink(prospecting_job_id=job_id, queue_id=queue_id)
        db.add(link)
    else:
        link.queue_id = queue_id
    db.flush()
    return link


def bind_conversation_queue(
    db: Session,
    conversation_id: int,
    queue_id: int,
    *,
    origin: str,
) -> ConversationQueueLink:
    link = db.scalar(
        select(ConversationQueueLink).where(
            ConversationQueueLink.conversation_id == conversation_id
        )
    )
    if not link:
        link = ConversationQueueLink(
            conversation_id=conversation_id,
            queue_id=queue_id,
            origin=origin,
        )
        db.add(link)
    else:
        link.queue_id = queue_id
        link.origin = origin
    db.flush()
    return link


def default_inbound_queue(
    db: Session,
    *,
    team: str,
    branch_id: int | None,
) -> ServiceQueue | None:
    stmt = select(ServiceQueue).where(
        ServiceQueue.active.is_(True),
        ServiceQueue.is_default_inbound.is_(True),
        ServiceQueue.team == team,
    )
    if branch_id:
        branch_queue = db.scalar(
            stmt.where(ServiceQueue.branch_id == branch_id)
            .order_by(ServiceQueue.id)
            .limit(1)
        )
        if branch_queue:
            return branch_queue
    return db.scalar(
        stmt.where(ServiceQueue.branch_id.is_(None))
        .order_by(ServiceQueue.id)
        .limit(1)
    )


def resolve_queue(
    db: Session,
    *,
    conversation_id: int | None = None,
    explicit_queue_id: int | None = None,
    lead_id: int | None = None,
    team: str = "B2B",
    branch_id: int | None = None,
) -> ServiceQueue | None:
    if explicit_queue_id:
        queue = get_queue(db, explicit_queue_id)
        if queue:
            return queue
    if conversation_id:
        queue = queue_for_conversation(db, conversation_id)
        if queue:
            return queue
    queue = preferred_queue_for_lead(db, lead_id)
    if queue:
        return queue
    return default_inbound_queue(db, team=team, branch_id=branch_id)


def queue_member_user_ids(db: Session, queue_id: int, *, active_only: bool = True) -> list[int]:
    stmt = select(ServiceQueueMember.user_id).where(
        ServiceQueueMember.queue_id == queue_id
    )
    if active_only:
        stmt = stmt.where(ServiceQueueMember.active.is_(True))
    return list(db.scalars(stmt))


def is_queue_member(db: Session, queue_id: int, user_id: int) -> bool:
    return bool(
        db.scalar(
            select(ServiceQueueMember.id).where(
                ServiceQueueMember.queue_id == queue_id,
                ServiceQueueMember.user_id == user_id,
                ServiceQueueMember.active.is_(True),
            )
        )
    )


def assign_queue_user(
    db: Session,
    queue: ServiceQueue,
) -> User | None:
    if queue.routing_strategy == "MANUAL":
        return None

    online = set(online_user_ids(db))
    if not online:
        return None

    memberships = list(
        db.scalars(
            select(ServiceQueueMember)
            .options(selectinload(ServiceQueueMember.user))
            .where(
                ServiceQueueMember.queue_id == queue.id,
                ServiceQueueMember.active.is_(True),
                ServiceQueueMember.user_id.in_(online),
            )
            .order_by(ServiceQueueMember.priority.asc(), ServiceQueueMember.id.asc())
        )
    )
    closer_user_ids = set(
        db.scalars(
            select(AIAgentProfile.user_id).where(
                AIAgentProfile.role_type == "CLOSER"
            )
        )
    )
    memberships = [
        item for item in memberships
        if item.user
        and item.user.active
        and item.user_id not in closer_user_ids
    ]
    if not memberships:
        return None

    ranked: list[tuple] = []
    for member in memberships:
        open_count = int(
            db.scalar(
                select(func.count(Conversation.id))
                .join(
                    ConversationQueueLink,
                    ConversationQueueLink.conversation_id == Conversation.id,
                )
                .where(
                    ConversationQueueLink.queue_id == queue.id,
                    Conversation.assigned_user_id == member.user_id,
                    Conversation.status.in_(OPEN_STATUSES),
                )
            )
            or 0
        )
        if queue.routing_strategy == "LEAST_OPEN":
            rank = (
                open_count,
                member.priority,
                _timestamp(member.last_assigned_at),
                member.id,
            )
        else:
            rank = (
                _timestamp(member.last_assigned_at),
                member.priority,
                open_count,
                member.id,
            )
        ranked.append((*rank, member))

    ranked.sort(key=lambda item: item[:-1])
    selected = ranked[0][-1]
    selected.last_assigned_at = utcnow()
    db.flush()
    return selected.user


def available_queues_for_user(
    db: Session,
    user: User,
    *,
    active_only: bool = True,
) -> list[ServiceQueue]:
    stmt = select(ServiceQueue).options(
        selectinload(ServiceQueue.branch),
        selectinload(ServiceQueue.members).selectinload(ServiceQueueMember.user),
    )
    if active_only:
        stmt = stmt.where(ServiceQueue.active.is_(True))

    if user.role == "CONSULTOR":
        stmt = stmt.join(
            ServiceQueueMember,
            ServiceQueueMember.queue_id == ServiceQueue.id,
        ).where(
            ServiceQueueMember.user_id == user.id,
            ServiceQueueMember.active.is_(True),
        )
    elif user.role == "SUPERVISOR":
        if user.branch_id:
            stmt = stmt.where(
                (ServiceQueue.branch_id == user.branch_id)
                | (ServiceQueue.branch_id.is_(None))
            )
        else:
            stmt = stmt.where(ServiceQueue.team == user.team)

    return list(db.scalars(stmt.order_by(ServiceQueue.name)).unique())


def queue_summary(db: Session, queue: ServiceQueue) -> dict[str, int]:
    member_ids = queue_member_user_ids(db, queue.id)
    online = set(online_user_ids(db))
    open_count = int(
        db.scalar(
            select(func.count(Conversation.id))
            .join(
                ConversationQueueLink,
                ConversationQueueLink.conversation_id == Conversation.id,
            )
            .where(
                ConversationQueueLink.queue_id == queue.id,
                Conversation.status.in_(OPEN_STATUSES),
            )
        )
        or 0
    )
    waiting_count = int(
        db.scalar(
            select(func.count(Conversation.id))
            .join(
                ConversationQueueLink,
                ConversationQueueLink.conversation_id == Conversation.id,
            )
            .where(
                ConversationQueueLink.queue_id == queue.id,
                Conversation.assigned_user_id.is_(None),
                Conversation.status.in_(OPEN_STATUSES - {"AGUARDANDO_CLIENTE"}),
            )
        )
        or 0
    )
    return {
        "members": len(member_ids),
        "online": len([item for item in member_ids if item in online]),
        "open": open_count,
        "waiting": waiting_count,
    }


# Modo de fila: a IA atende só quando nenhum vendedor da fila está online.
AI_WHEN_OFFLINE = "AI_WHEN_OFFLINE"
# Fila comercial: a IA só entra sem vendedor online na fila ou depois de 10 min
# sem o vendedor interagir com o lead. Fila da IA continua em AI_WHEN_OFFLINE.
AI_WHEN_HUMAN_IDLE = "AI_WHEN_HUMAN_IDLE"
AI_WHEN_OFFLINE_MODES = (AI_WHEN_OFFLINE, AI_WHEN_HUMAN_IDLE)
AI_OWNED_STATES = ("AI_ACTIVE", "CLOSING")


def human_online_in_queue(db: Session, queue_id: int | None) -> bool:
    """Algum vendedor de verdade (não um agente de IA) da fila está online."""
    if not queue_id:
        return False
    online = set(online_user_ids(db))
    if not online:
        return False
    ai_users = {user_id for user_id in db.scalars(select(AIAgentProfile.user_id)) if user_id}
    members = db.scalars(
        select(ServiceQueueMember.user_id)
        .join(User, User.id == ServiceQueueMember.user_id)
        .where(
            ServiceQueueMember.queue_id == queue_id,
            ServiceQueueMember.active.is_(True),
            User.active.is_(True),
            ServiceQueueMember.user_id.in_(online),
        )
    )
    return any(user_id not in ai_users for user_id in members)


def ai_owns_conversation(db: Session, conversation_id: int | None) -> bool:
    """A IA já começou este atendimento (SDR ou Vendedor IA) e segue até o fim."""
    if not conversation_id:
        return False
    state = db.scalar(
        select(AIConversationState.state).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    return state in AI_OWNED_STATES
