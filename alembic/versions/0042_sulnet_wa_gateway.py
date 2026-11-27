"""Motor WhatsApp Sulnet: instancias proprias, vinculos e idempotencia de webhooks.

Revision ID: 0042_sulnet_wa_gateway
Revises: 0041_voice_native_techprefix
"""
from alembic import op
import sqlalchemy as sa

revision = "0042_sulnet_wa_gateway"
down_revision = "0041_voice_native_techprefix"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "sulnet_wa_instances" not in existing:
        op.create_table(
            "sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("session_id", sa.String(80), nullable=False, unique=True),
            sa.Column("phone_label", sa.String(32), nullable=False, server_default=""),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "service_queue_sulnet_wa_instances" not in existing:
        op.create_table(
            "service_queue_sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("queue_id", sa.Integer(), sa.ForeignKey("service_queues.id", ondelete="CASCADE"), unique=True, nullable=False),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="RESTRICT"), unique=True, nullable=False),
        )
    if "conversation_sulnet_wa_instances" not in existing:
        op.create_table(
            "conversation_sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), unique=True, nullable=False),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="RESTRICT"), nullable=False),
        )
    if "sulnet_wa_receipts" not in existing:
        op.create_table(
            "sulnet_wa_receipts",
            sa.Column("event_id", sa.String(64), primary_key=True),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="CASCADE"), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table("sulnet_wa_receipts")
    op.drop_table("conversation_sulnet_wa_instances")
    op.drop_table("service_queue_sulnet_wa_instances")
    op.drop_table("sulnet_wa_instances")
