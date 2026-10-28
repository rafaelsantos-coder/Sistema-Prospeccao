from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutils import utcnow


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="branch")
    leads: Mapped[list["Lead"]] = relationship(back_populates="branch")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="CONSULTOR", index=True)
    team: Mapped[str] = mapped_column(String(30), default="LOJA", index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    branch: Mapped[Branch | None] = relationship(back_populates="users")
    assigned_leads: Mapped[list["Lead"]] = relationship(
        foreign_keys="Lead.assigned_user_id", back_populates="assigned_user"
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_pipeline", "stage", "team", "branch_id"),
        Index("ix_leads_followup", "next_action_at", "stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    business_name: Mapped[str] = mapped_column(String(180), index=True)
    contact_name: Mapped[str] = mapped_column(String(150), default="")
    cnpj: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment: Mapped[str] = mapped_column(String(120), default="Não informado", index=True)
    city: Mapped[str] = mapped_column(String(120), default="Santa Rosa", index=True)
    neighborhood: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="MANUAL", index=True)
    origin_channel: Mapped[str] = mapped_column(String(40), default="WHATSAPP", index=True)
    team: Mapped[str] = mapped_column(String(30), default="LOJA", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="NOVO", index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    legacy_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[str] = mapped_column(String(20), default="FRIO", index=True)
    priority: Mapped[str] = mapped_column(String(20), default="NORMAL", index=True)
    potential: Mapped[str] = mapped_column(Text, default="")
    estimated_mrr: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(Text, default="")
    is_paid_traffic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    lgpd_legal_basis: Mapped[str] = mapped_column(String(80), default="LEGITIMO_INTERESSE")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    raw_data: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    branch: Mapped[Branch | None] = relationship(back_populates="leads")
    assigned_user: Mapped[User | None] = relationship(
        foreign_keys=[assigned_user_id], back_populates="assigned_leads"
    )
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="desc(Activity.created_at)"
    )


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="CONCLUIDA", index=True)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    lead: Mapped[Lead] = relationship(back_populates="activities")
    user: Mapped[User | None] = relationship()


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (UniqueConstraint("team", "metric", "branch_id", name="uq_target_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team: Mapped[str] = mapped_column(String(30), index=True)
    metric: Mapped[str] = mapped_column(String(30), index=True)
    daily_target: Mapped[int] = mapped_column(Integer)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    branch: Mapped[Branch | None] = relationship()


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(180))
    segment: Mapped[str | None] = mapped_column(String(120), nullable=True)
    team: Mapped[str | None] = mapped_column(String(30), nullable=True)
    channel: Mapped[str] = mapped_column(String(30), default="WHATSAPP")
    status: Mapped[str] = mapped_column(String(30), default="RASCUNHO", index=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=30)
    sequence_json: Mapped[str] = mapped_column(Text, default="[]")
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    branch: Mapped[Branch | None] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])


class ProspectingJob(Base):
    __tablename__ = "prospecting_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    segment: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120))
    limit: Mapped[int] = mapped_column(Integer, default=20)
    team: Mapped[str] = mapped_column(String(30), default="B2B")
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", index=True)
    found_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    created_by: Mapped[User | None] = relationship()
    branch: Mapped[Branch | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped[User | None] = relationship()


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), default="")
    business_name: Mapped[str] = mapped_column(String(180), default="")
    email: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lead: Mapped[Lead | None] = relationship()
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="contact")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_inbox", "assigned_user_id", "status", "last_message_at"),
        Index("ix_conversations_branch_team", "branch_id", "team", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    team: Mapped[str] = mapped_column(String(30), default="B2B", index=True)
    source: Mapped[str] = mapped_column(String(40), default="WHATSAPP", index=True)
    status: Mapped[str] = mapped_column(String(30), default="NOVO", index=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_preview: Mapped[str] = mapped_column(String(240), default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    contact: Mapped[Contact] = relationship(back_populates="conversations")
    lead: Mapped[Lead | None] = relationship()
    assigned_user: Mapped[User | None] = relationship(foreign_keys=[assigned_user_id])
    branch: Mapped[Branch | None] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    assignments: Mapped[list["ConversationAssignment"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationAssignment.created_at",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(20), index=True)
    message_type: Mapped[str] = mapped_column(String(30), default="TEXT", index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(180), nullable=True, unique=True, index=True)
    provider_zaap_id: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    contact: Mapped[Contact] = relationship()
    sender_user: Mapped[User | None] = relationship()


class MessageEvent(Base):
    __tablename__ = "message_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), default="STATUS", index=True)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    message: Mapped[Message | None] = relationship()


class ConversationAssignment(Base):
    __tablename__ = "conversation_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(40), default="ASSIGNED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="assignments")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    assigned_by: Mapped[User | None] = relationship(foreign_keys=[assigned_by_id])


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_recipient"),
        Index("ix_campaign_recipients_queue", "status", "scheduled_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", index=True)
    message_body: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign: Mapped[Campaign] = relationship()
    lead: Mapped[Lead] = relationship()
    conversation: Mapped[Conversation | None] = relationship()


class WhatsAppPresence(Base):
    __tablename__ = "whatsapp_presence"
    __table_args__ = (
        Index("ix_whatsapp_presence_online", "status", "last_heartbeat_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="OFFLINE", index=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship()


class ServiceQueue(Base):
    __tablename__ = "service_queues"
    __table_args__ = (
        Index("ix_service_queues_scope", "team", "branch_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    team: Mapped[str] = mapped_column(String(30), default="B2B", index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    routing_strategy: Mapped[str] = mapped_column(String(30), default="ROUND_ROBIN", index=True)
    is_default_inbound: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    branch: Mapped[Branch | None] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    members: Mapped[list["ServiceQueueMember"]] = relationship(
        back_populates="queue",
        cascade="all, delete-orphan",
        order_by="ServiceQueueMember.id",
    )


class ServiceQueueMember(Base):
    __tablename__ = "service_queue_members"
    __table_args__ = (
        UniqueConstraint("queue_id", "user_id", name="uq_service_queue_member"),
        Index("ix_service_queue_members_active", "queue_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    queue: Mapped[ServiceQueue] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class ConversationQueueLink(Base):
    __tablename__ = "conversation_queue_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(40), default="INBOUND", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    conversation: Mapped[Conversation] = relationship()
    queue: Mapped[ServiceQueue] = relationship()


class CampaignQueueLink(Base):
    __tablename__ = "campaign_queue_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    campaign: Mapped[Campaign] = relationship()
    queue: Mapped[ServiceQueue] = relationship()


class ProspectingQueueLink(Base):
    __tablename__ = "prospecting_queue_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospecting_job_id: Mapped[int] = mapped_column(
        ForeignKey("prospecting_jobs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    prospecting_job: Mapped[ProspectingJob] = relationship()
    queue: Mapped[ServiceQueue] = relationship()


class LeadQueuePreference(Base):
    __tablename__ = "lead_queue_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40), default="MANUAL")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lead: Mapped[Lead] = relationship()
    queue: Mapped[ServiceQueue] = relationship()


class CampaignDispatch(Base):
    __tablename__ = "campaign_dispatches"
    __table_args__ = (
        Index("ix_campaign_dispatches_status_next", "status", "next_send_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    queue_id: Mapped[int] = mapped_column(
        ForeignKey("service_queues.id", ondelete="RESTRICT"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="RASCUNHO", index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    daily_limit: Mapped[int] = mapped_column(Integer, default=100)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    import_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign: Mapped[Campaign] = relationship()
    queue: Mapped[ServiceQueue] = relationship()
    created_by: Mapped[User | None] = relationship()


class CampaignDispatchRecipient(Base):
    __tablename__ = "campaign_dispatch_recipients"
    __table_args__ = (
        UniqueConstraint("dispatch_id", "phone", name="uq_campaign_dispatch_recipient_phone"),
        Index("ix_campaign_dispatch_recipients_queue", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("campaign_dispatches.id", ondelete="CASCADE"),
        index=True,
    )
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(180), default="")
    phone: Mapped[str] = mapped_column(String(30), index=True)
    message_body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    dispatch: Mapped[CampaignDispatch] = relationship()
    lead: Mapped[Lead | None] = relationship()
    conversation: Mapped[Conversation | None] = relationship()


class AIAgentProfile(Base):
    __tablename__ = "ai_agent_profiles"
    __table_args__ = (
        Index("ix_ai_agent_profiles_active", "active", "execution_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), default="SulIA SDR")
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    role_type: Mapped[str] = mapped_column(String(30), default="SDR", index=True)
    execution_mode: Mapped[str] = mapped_column(String(30), default="SHADOW", index=True)
    model: Mapped[str] = mapped_column(String(80), default="gpt-6-luna")
    reasoning_effort: Mapped[str] = mapped_column(String(20), default="low")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    public_name: Mapped[str] = mapped_column(String(120), default="Sulnet")
    business_goal: Mapped[str] = mapped_column(
        Text,
        default=(
            "Atuar como SDR da Sulnet: entender a necessidade, qualificar a oportunidade, "
            "tratar dúvidas comerciais e recomendar handoff para um vendedor quando houver intenção real."
        ),
    )
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    qualification_handoff_score: Mapped[int] = mapped_column(Integer, default=70)
    max_history_messages: Mapped[int] = mapped_column(Integer, default=24)
    max_knowledge_chars: Mapped[int] = mapped_column(Integer, default=12000)
    tools_json: Mapped[str] = mapped_column(Text, default="[]")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])


class AIQueueBinding(Base):
    __tablename__ = "ai_queue_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    queue_id: Mapped[int] = mapped_column(
        ForeignKey("service_queues.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("ai_agent_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    handling_mode: Mapped[str] = mapped_column(String(30), default="AI_FIRST", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    fallback_to_human: Mapped[bool] = mapped_column(Boolean, default=True)
    handoff_score_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    queue: Mapped[ServiceQueue] = relationship()
    agent: Mapped[AIAgentProfile] = relationship()


class AIKnowledgeItem(Base):
    __tablename__ = "ai_knowledge_items"
    __table_args__ = (
        Index("ix_ai_knowledge_agent_active", "agent_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("ai_agent_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(180), index=True)
    category: Mapped[str] = mapped_column(String(80), default="GERAL", index=True)
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[str] = mapped_column(String(500), default="")
    source_type: Mapped[str] = mapped_column(String(40), default="MANUAL")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    agent: Mapped[AIAgentProfile] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])


class AIJob(Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (
        Index("ix_ai_jobs_worker", "status", "available_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("ai_agent_profiles.id"), index=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("service_queues.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    agent: Mapped[AIAgentProfile] = relationship()
    queue: Mapped[ServiceQueue] = relationship()
    conversation: Mapped[Conversation] = relationship()
    message: Mapped[Message] = relationship()


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        Index("ix_ai_runs_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("ai_jobs.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("ai_agent_profiles.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    execution_mode: Mapped[str] = mapped_column(String(30), default="SHADOW", index=True)
    model: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="PROCESSANDO", index=True)
    intent: Mapped[str] = mapped_column(String(60), default="UNKNOWN", index=True)
    qualification_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    temperature: Mapped[str] = mapped_column(String(20), default="FRIO", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    handoff_recommended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    handoff_reason: Mapped[str] = mapped_column(Text, default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    input_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[AIJob] = relationship()
    agent: Mapped[AIAgentProfile] = relationship()
    conversation: Mapped[Conversation] = relationship()
    message: Mapped[Message] = relationship()


class AIConversationState(Base):
    __tablename__ = "ai_conversation_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    agent_id: Mapped[int] = mapped_column(ForeignKey("ai_agent_profiles.id"), index=True)
    state: Mapped[str] = mapped_column(String(30), default="AI_ACTIVE", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    qualification_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    temperature: Mapped[str] = mapped_column(String(20), default="FRIO", index=True)
    last_intent: Mapped[str] = mapped_column(String(60), default="UNKNOWN")
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("ai_runs.id"), nullable=True)
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship()
    agent: Mapped[AIAgentProfile] = relationship()
    last_run: Mapped[AIRun | None] = relationship(foreign_keys=[last_run_id])
