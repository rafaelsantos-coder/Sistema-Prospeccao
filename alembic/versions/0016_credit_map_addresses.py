"""Endereços de clientes aprovados do mapa de crédito.

Revision ID: 0016_credit_map_addresses
Revises: 0015_credit_map_streets
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_credit_map_addresses"
down_revision = "0015_credit_map_streets"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("credit_map_addresses"):
        return
    op.create_table(
        "credit_map_addresses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("city", sa.String(120), nullable=False),
        sa.Column("neighborhood", sa.String(180), nullable=False),
        sa.Column("street", sa.String(100), nullable=False),
        sa.Column("number", sa.String(12), nullable=False, server_default=""),
        sa.Column("cep", sa.String(8), nullable=False, server_default=""),
        sa.Column("month", sa.String(12), nullable=False),
        sa.Column("signed", sa.Boolean(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_credit_map_addresses_city", "credit_map_addresses", ["city"])


def downgrade():
    op.drop_table("credit_map_addresses")
