"""Guarda a lead do CRM aberta na primeira mensagem do cliente.

Revision ID: 0008_conversation_crm_opportunity
Revises: 0007_mfa_schema_repair
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_conversation_crm_opportunity"
down_revision = "0007_mfa_schema_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("conversations")}
    if "crm_opportunity_id" not in columns:
        op.add_column("conversations", sa.Column("crm_opportunity_id", sa.Integer(), nullable=True))
        op.create_index(
            "ix_conversations_crm_opportunity_id",
            "conversations",
            ["crm_opportunity_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_conversations_crm_opportunity_id", table_name="conversations")
    op.drop_column("conversations", "crm_opportunity_id")
