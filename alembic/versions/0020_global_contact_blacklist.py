"""Blacklist global de contatos para qualquer fila/instancia de mensagens.

Revision ID: 0020_global_contact_blacklist
Revises: 0019_voice_dialer
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_global_contact_blacklist"
down_revision = "0019_voice_dialer"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("global_contact_blacklist"):
        op.create_table(
            "global_contact_blacklist",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("phone", sa.String(20), nullable=False),
            sa.Column("name", sa.String(150), nullable=False, server_default=""),
            sa.Column("reason", sa.String(300), nullable=False, server_default="Não deseja receber mensagens"),
            sa.Column("source", sa.String(24), nullable=False, server_default="MANUAL"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("removed_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_global_contact_blacklist_phone", "global_contact_blacklist", ["phone"], unique=True)
        op.create_index("ix_global_contact_blacklist_active", "global_contact_blacklist", ["active"])


def downgrade():
    op.drop_index("ix_global_contact_blacklist_active", table_name="global_contact_blacklist")
    op.drop_index("ix_global_contact_blacklist_phone", table_name="global_contact_blacklist")
    op.drop_table("global_contact_blacklist")
