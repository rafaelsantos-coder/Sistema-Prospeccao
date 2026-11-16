"""Coordenadas de origem do CRM para os marcadores aprovados.

Revision ID: 0017_credit_map_coords
Revises: 0016_credit_map_addresses
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_credit_map_coords"
down_revision = "0016_credit_map_addresses"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("credit_map_addresses"):
        return
    existing = {c["name"] for c in inspector.get_columns("credit_map_addresses")}
    if "lat" not in existing:
        op.add_column("credit_map_addresses", sa.Column("lat", sa.Float(), nullable=True))
    if "lng" not in existing:
        op.add_column("credit_map_addresses", sa.Column("lng", sa.Float(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("credit_map_addresses"):
        return
    existing = {c["name"] for c in inspector.get_columns("credit_map_addresses")}
    if "lng" in existing:
        op.drop_column("credit_map_addresses", "lng")
    if "lat" in existing:
        op.drop_column("credit_map_addresses", "lat")
