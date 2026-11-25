"""Fila persistente e isolada de eventos do Instagram; não é um contato WhatsApp."""
from sqlalchemy import Column, DateTime, Index, String, Table, Text

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
