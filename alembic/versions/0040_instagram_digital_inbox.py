"""Dedicated Instagram Digital inbox; no WhatsApp data changes."""
from alembic import op
import sqlalchemy as sa

revision = "0040_instagram_digital_inbox"
down_revision = "0039_instagram_ingress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "instagram_inbox_members" not in existing:
        op.create_table("instagram_inbox_members",
            sa.Column("account_id", sa.String(80), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True))
    if "instagram_inbox_threads" not in existing:
        op.create_table("instagram_inbox_threads",
            sa.Column("thread_id", sa.String(64), primary_key=True),
            sa.Column("account_id", sa.String(80), nullable=False),
            sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_ig_inbox_account_owner", "instagram_inbox_threads",
            ["account_id", "assigned_user_id", "last_inbound_at"])
    if "instagram_inbox_messages" not in existing:
        op.create_table("instagram_inbox_messages",
            sa.Column("message_key", sa.String(64), primary_key=True),
            sa.Column("thread_id", sa.String(64), sa.ForeignKey("instagram_inbox_threads.thread_id", ondelete="CASCADE"), nullable=False),
            sa.Column("body_encrypted", sa.Text(), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_ig_inbox_thread_time", "instagram_inbox_messages", ["thread_id", "received_at"])


def downgrade() -> None:
    op.drop_table("instagram_inbox_messages")
    op.drop_table("instagram_inbox_threads")
    op.drop_table("instagram_inbox_members")
