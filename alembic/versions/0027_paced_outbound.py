"""Outbox duravel com janela de 30 segundos por numero Z-API.

Revision ID: 0027_paced_outbound
Revises: 0026_user_access_permissions
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_paced_outbound"
down_revision = "0026_user_access_permissions"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("paced_outbound_messages"):
        op.create_table(
            "paced_outbound_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "message_id", sa.Integer(),
                sa.ForeignKey("messages.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("instance_key", sa.String(180), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
            sa.Column("caption", sa.Text(), nullable=False, server_default=""),
            sa.Column("filename", sa.String(240), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_paced_outbound_messages_message_id",
            "paced_outbound_messages", ["message_id"], unique=True,
        )
        op.create_index(
            "ix_paced_outbound_due", "paced_outbound_messages",
            ["status", "scheduled_at"],
        )
        op.create_index(
            "ix_paced_outbound_instance_slot", "paced_outbound_messages",
            ["instance_key", "scheduled_at"],
        )


def downgrade():
    op.drop_index("ix_paced_outbound_instance_slot", table_name="paced_outbound_messages")
    op.drop_index("ix_paced_outbound_due", table_name="paced_outbound_messages")
    op.drop_index("ix_paced_outbound_messages_message_id", table_name="paced_outbound_messages")
    op.drop_table("paced_outbound_messages")
