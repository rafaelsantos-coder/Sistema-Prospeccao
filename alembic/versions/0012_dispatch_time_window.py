"""Janela diária de envio (início e fim) nos disparos.

Revision ID: 0012_dispatch_time_window
Revises: 0011_credit_geo_aggregates
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_dispatch_time_window"
down_revision = "0011_credit_geo_aggregates"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("campaign_dispatches", sa.Column("window_start", sa.String(5), nullable=True))
    op.add_column("campaign_dispatches", sa.Column("window_end", sa.String(5), nullable=True))


def downgrade():
    op.drop_column("campaign_dispatches", "window_end")
    op.drop_column("campaign_dispatches", "window_start")
