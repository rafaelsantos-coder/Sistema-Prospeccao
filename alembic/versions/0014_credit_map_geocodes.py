"""Cache de geocodificação do mapa de crédito (OpenStreetMap).

Revision ID: 0014_credit_map_geocodes
Revises: 0013_telegram_channel
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_credit_map_geocodes"
down_revision = "0013_telegram_channel"
branch_labels = None
depends_on = None


def upgrade():
    # Baseline usa create_all com os modelos atuais; em banco novo a tabela já existe.
    if sa.inspect(op.get_bind()).has_table("credit_map_geocodes"):
        return
    op.create_table(
        "credit_map_geocodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("city", sa.String(120), nullable=False),
        sa.Column("neighborhood", sa.String(180), nullable=False, server_default=""),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="FOUND"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("city", "neighborhood", name="uq_credit_map_geocode"),
    )


def downgrade():
    op.drop_table("credit_map_geocodes")
