"""Conexão WhatsApp pela API oficial da Meta (Cloud API).

Somente tabelas novas. Nenhuma tabela, coluna ou dado da Z-API é alterado:
as duas integrações convivem e cada fila escolhe por qual canal envia.

Revision ID: 0009_meta_cloud_api
Revises: 0008_prospecting_execution
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_meta_cloud_api"
down_revision = "0008_prospecting_execution"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    if "meta_whatsapp_numbers" not in tables:
        op.create_table(
            "meta_whatsapp_numbers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("phone_label", sa.String(80), nullable=False, server_default=""),
            sa.Column("verified_name", sa.String(180), nullable=False, server_default=""),
            sa.Column("quality_rating", sa.String(30), nullable=False, server_default=""),
            sa.Column("phone_number_id", sa.String(64), nullable=False),
            sa.Column("waba_id", sa.String(64), nullable=False),
            sa.Column("graph_version", sa.String(16), nullable=False, server_default=""),
            sa.Column("access_token_encrypted", sa.Text(), nullable=False),
            sa.Column("app_secret_encrypted", sa.Text(), nullable=False),
            sa.Column("verify_token_encrypted", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("webhook_subscribed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("templates_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_meta_whatsapp_numbers_name", "meta_whatsapp_numbers", ["name"])
        op.create_index(
            "ix_meta_whatsapp_numbers_phone_number_id",
            "meta_whatsapp_numbers",
            ["phone_number_id"],
            unique=True,
        )
        op.create_index("ix_meta_whatsapp_numbers_waba_id", "meta_whatsapp_numbers", ["waba_id"])
        op.create_index("ix_meta_whatsapp_numbers_active", "meta_whatsapp_numbers", ["active"])

    if "service_queue_meta_numbers" not in tables:
        op.create_table(
            "service_queue_meta_numbers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "queue_id",
                sa.Integer(),
                sa.ForeignKey("service_queues.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "meta_number_id",
                sa.Integer(),
                sa.ForeignKey("meta_whatsapp_numbers.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_service_queue_meta_numbers_queue_id",
            "service_queue_meta_numbers",
            ["queue_id"],
            unique=True,
        )
        op.create_index(
            "ix_service_queue_meta_numbers_meta_number_id",
            "service_queue_meta_numbers",
            ["meta_number_id"],
        )

    if "conversation_meta_numbers" not in tables:
        op.create_table(
            "conversation_meta_numbers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "conversation_id",
                sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "meta_number_id",
                sa.Integer(),
                sa.ForeignKey("meta_whatsapp_numbers.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_conversation_meta_numbers_conversation_id",
            "conversation_meta_numbers",
            ["conversation_id"],
            unique=True,
        )
        op.create_index(
            "ix_conversation_meta_numbers_meta_number_id",
            "conversation_meta_numbers",
            ["meta_number_id"],
        )

    if "meta_message_templates" not in tables:
        op.create_table(
            "meta_message_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "meta_number_id",
                sa.Integer(),
                sa.ForeignKey("meta_whatsapp_numbers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("meta_template_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("name", sa.String(512), nullable=False),
            sa.Column("language", sa.String(20), nullable=False),
            sa.Column("category", sa.String(30), nullable=False, server_default=""),
            sa.Column("status", sa.String(30), nullable=False, server_default=""),
            sa.Column("parameter_format", sa.String(20), nullable=False, server_default="POSITIONAL"),
            sa.Column("header_format", sa.String(20), nullable=False, server_default=""),
            sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("components_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "meta_number_id",
                "name",
                "language",
                name="uq_meta_template_number_name_lang",
            ),
        )
        op.create_index(
            "ix_meta_message_templates_meta_number_id",
            "meta_message_templates",
            ["meta_number_id"],
        )
        op.create_index("ix_meta_message_templates_name", "meta_message_templates", ["name"])
        op.create_index("ix_meta_message_templates_status", "meta_message_templates", ["status"])


def downgrade() -> None:
    tables = _existing_tables()
    for table in (
        "meta_message_templates",
        "conversation_meta_numbers",
        "service_queue_meta_numbers",
        "meta_whatsapp_numbers",
    ):
        if table in tables:
            op.drop_table(table)
