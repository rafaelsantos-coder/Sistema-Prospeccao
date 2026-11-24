"""Cadastro de campanhas de anúncio para origem automática no CRM.

Revision ID: 0036_origin_campaigns
Revises: 0035_lead_prospecting_job
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_origin_campaigns"
down_revision = "0035_lead_prospecting_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    tables = set(sa.inspect(db).get_table_names())
    if "origin_campaigns" in tables:
        return
    op.create_table(
        "origin_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("match_text", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("crm_origin", sa.String(length=180), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_origin_campaigns_active", "origin_campaigns", ["active"])


def downgrade() -> None:
    db = op.get_bind()
    tables = set(sa.inspect(db).get_table_names())
    if "origin_campaigns" not in tables:
        return
    indexes = {i["name"] for i in sa.inspect(db).get_indexes("origin_campaigns")}
    if "ix_origin_campaigns_active" in indexes:
        op.drop_index("ix_origin_campaigns_active", table_name="origin_campaigns")
    op.drop_table("origin_campaigns")
