"""Base de caixas NAP para o mapa territorial de crédito."""

import sqlalchemy as sa

from alembic import op

revision = "0029_credit_map_network"
down_revision = "0028_fila_ia_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("credit_map_network"):
        op.create_table(
            "credit_map_network",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("credit_map_network")
