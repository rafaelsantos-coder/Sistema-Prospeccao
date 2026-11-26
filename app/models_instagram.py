"""Fila persistente e isolada de eventos do Instagram; não é um contato WhatsApp."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Table, Text

from app.database import Base

instagram_webhook_events = Table(
    "instagram_webhook_events",
    Base.metadata,
    Column("event_hash", String(64), primary_key=True),
    Column("account_id", String(80), nullable=False),
    Column("event_kind", String(20), nullable=False),
    Column("payload_encrypted", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Index("ix_instagram_webhook_events_account_received", "account_id", "received_at"),
)


# Membership and conversations are separate from the WhatsApp queue models.
instagram_inbox_members = Table(
    "instagram_inbox_members", Base.metadata,
    Column("account_id", String(80), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)
instagram_inbox_threads = Table(
    "instagram_inbox_threads", Base.metadata,
    Column("thread_id", String(64), primary_key=True),
    Column("account_id", String(80), nullable=False),
    Column("assigned_user_id", Integer, ForeignKey("users.id"), nullable=True),
    Column("last_inbound_at", DateTime(timezone=True), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Index("ix_ig_inbox_account_owner", "account_id", "assigned_user_id", "last_inbound_at"),
)
instagram_inbox_messages = Table(
    "instagram_inbox_messages", Base.metadata,
    Column("message_key", String(64), primary_key=True),
    Column("thread_id", String(64), ForeignKey("instagram_inbox_threads.thread_id", ondelete="CASCADE"), nullable=False),
    Column("body_encrypted", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Index("ix_ig_inbox_thread_time", "thread_id", "received_at"),
)
