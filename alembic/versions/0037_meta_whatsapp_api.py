"""WhatsApp Cloud API oficial Meta: contas e vínculo com filas.

Revision ID: 0037_meta_whatsapp
Revises: 0036_origin_campaigns
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_meta_whatsapp"
down_revision = "0036_origin_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    tables = set(sa.inspect(db).get_table_names())

    if "meta_whatsapp_accounts" not in tables:
        op.create_table(
            "meta_whatsapp_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("phone_label", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("phone_number_id", sa.String(length=80), nullable=False),
            sa.Column("waba_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("access_token_encrypted", sa.Text(), nullable=False),
            sa.Column("app_secret_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("verify_token", sa.String(length=120), nullable=False),
            sa.Column("webhook_key", sa.String(length=120), nullable=False),
            sa.Column("graph_api_version", sa.String(length=20), nullable=False, server_default="v21.0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("phone_number_id"),
            sa.UniqueConstraint("webhook_key"),
        )
        op.create_index("ix_meta_whatsapp_accounts_name", "meta_whatsapp_accounts", ["name"])
        op.create_index(
            "ix_meta_whatsapp_accounts_phone_number_id",
            "meta_whatsapp_accounts",
            ["phone_number_id"],
        )
        op.create_index(
            "ix_meta_whatsapp_accounts_webhook_key",
            "meta_whatsapp_accounts",
            ["webhook_key"],
        )
        op.create_index("ix_meta_whatsapp_accounts_active", "meta_whatsapp_accounts", ["active"])

    tables = set(sa.inspect(db).get_table_names())
    if "service_queue_meta_accounts" not in tables:
        op.create_table(
            "service_queue_meta_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "queue_id",
                sa.Integer(),
                sa.ForeignKey("service_queues.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "meta_account_id",
                sa.Integer(),
                sa.ForeignKey("meta_whatsapp_accounts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("queue_id"),
        )
        op.create_index(
            "ix_service_queue_meta_accounts_queue_id",
            "service_queue_meta_accounts",
            ["queue_id"],
        )
        op.create_index(
            "ix_service_queue_meta_accounts_meta_account_id",
            "service_queue_meta_accounts",
            ["meta_account_id"],
        )

    tables = set(sa.inspect(db).get_table_names())
    if "conversation_meta_accounts" not in tables:
        op.create_table(
            "conversation_meta_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "conversation_id",
                sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "meta_account_id",
                sa.Integer(),
                sa.ForeignKey("meta_whatsapp_accounts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("conversation_id"),
        )
        op.create_index(
            "ix_conversation_meta_accounts_conversation_id",
            "conversation_meta_accounts",
            ["conversation_id"],
        )
        op.create_index(
            "ix_conversation_meta_accounts_meta_account_id",
            "conversation_meta_accounts",
            ["meta_account_id"],
        )


def downgrade() -> None:
    op.drop_table("conversation_meta_accounts")
    op.drop_table("service_queue_meta_accounts")
    op.drop_table("meta_whatsapp_accounts")
