"""Insights e etiquetas comerciais por conversa, sem acionar atendimento.

Revision ID: 0018_commercial_intelligence
Revises: 0017_credit_map_coords
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_commercial_intelligence"
down_revision = "0017_credit_map_coords"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("conversation_commercial_insights"):
        op.create_table(
            "conversation_commercial_insights",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("temperature", sa.String(12), nullable=False, server_default="INDEFINIDO"),
            sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stage", sa.String(35), nullable=False, server_default="PRIMEIRO_CONTATO"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("rationale", sa.String(900), nullable=False, server_default=""),
            sa.Column("next_action", sa.String(450), nullable=False, server_default=""),
            sa.Column("next_action_owner", sa.String(20), nullable=False, server_default="VENDEDOR"),
            sa.Column("source", sa.String(12), nullable=False, server_default="REGRAS"),
            sa.Column("last_analyzed_message_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_conversation_commercial_insights_conversation_id", "conversation_commercial_insights", ["conversation_id"], unique=True)
        op.create_index("ix_conversation_commercial_insights_temperature", "conversation_commercial_insights", ["temperature"])
    if not sa.inspect(bind).has_table("conversation_commercial_tags"):
        op.create_table(
            "conversation_commercial_tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("code", sa.String(45), nullable=False),
            sa.Column("source", sa.String(12), nullable=False, server_default="IA"),
            sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("changed_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("conversation_id", "code", name="uq_conversation_commercial_tag"),
        )
        op.create_index("ix_conversation_commercial_tags_conversation_id", "conversation_commercial_tags", ["conversation_id"])
        op.create_index("ix_conversation_commercial_tags_code", "conversation_commercial_tags", ["code"])


def downgrade():
    op.drop_index("ix_conversation_commercial_tags_code", table_name="conversation_commercial_tags")
    op.drop_index("ix_conversation_commercial_tags_conversation_id", table_name="conversation_commercial_tags")
    op.drop_table("conversation_commercial_tags")
    op.drop_index("ix_conversation_commercial_insights_temperature", table_name="conversation_commercial_insights")
    op.drop_index("ix_conversation_commercial_insights_conversation_id", table_name="conversation_commercial_insights")
    op.drop_table("conversation_commercial_insights")
