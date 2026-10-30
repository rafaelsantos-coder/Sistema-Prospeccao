"""Preço monetário como NUMERIC.

Revision ID: 0003_money_numeric
Revises: 0002_auth_security
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_money_numeric"
down_revision = "0002_auth_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]: item
        for item in sa.inspect(bind).get_columns("ai_closing_states")
    }
    current = columns.get("offer_price")
    if not current:
        return
    type_name = current["type"].__class__.__name__.casefold()
    if "numeric" in type_name or "decimal" in type_name:
        return
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "ai_closing_states",
            "offer_price",
            existing_type=current["type"],
            type_=sa.Numeric(12, 2),
            existing_nullable=True,
            postgresql_using="ROUND(offer_price::numeric, 2)",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "ai_closing_states",
            "offer_price",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Float(),
            existing_nullable=True,
            postgresql_using="offer_price::double precision",
        )
