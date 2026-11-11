"""Canal Telegram: vínculo lead/chat e canal do disparo.

Revision ID: 0013_telegram_channel
Revises: 0012_dispatch_time_window
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_telegram_channel"
down_revision = "0012_dispatch_time_window"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Baseline usa create_all com os modelos atuais; em banco novo tudo já existe.
    if not inspector.has_table("telegram_links"):
        op.create_table(
            "telegram_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token", sa.String(40), nullable=False),
            sa.Column("chat_id", sa.String(40), nullable=True),
            sa.Column("username", sa.String(80), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="PENDENTE"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_telegram_links_lead_id", "telegram_links", ["lead_id"])
        op.create_index("ix_telegram_links_token", "telegram_links", ["token"], unique=True)
        op.create_index("ix_telegram_links_chat_id", "telegram_links", ["chat_id"], unique=True)
        op.create_index("ix_telegram_links_status", "telegram_links", ["status"])
    columns = {c["name"] for c in inspector.get_columns("campaign_dispatches")}
    if "channel" not in columns:
        op.add_column(
            "campaign_dispatches",
            sa.Column("channel", sa.String(20), nullable=False, server_default="WHATSAPP"),
        )


def downgrade():
    op.drop_column("campaign_dispatches", "channel")
    op.drop_table("telegram_links")
