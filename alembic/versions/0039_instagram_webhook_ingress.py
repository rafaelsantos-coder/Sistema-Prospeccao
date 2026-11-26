"""Entrada isolada e criptografada dos eventos Instagram."""
from alembic import op
import sqlalchemy as sa

revision = "0039_instagram_ingress"
down_revision = "0038_meta_digital_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "instagram_webhook_events" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "instagram_webhook_events",
        sa.Column("event_hash", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(80), nullable=False),
        sa.Column("event_kind", sa.String(20), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_instagram_webhook_events_account_received",
        "instagram_webhook_events",
        ["account_id", "received_at"],
    )


def downgrade() -> None:
    # A reversão desta revisão remove exclusivamente a caixa de eventos Instagram.
    op.drop_table("instagram_webhook_events")
