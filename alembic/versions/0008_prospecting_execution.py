"""Plano de execução na prospecção (busca + disparo opcional).

Revision ID: 0008_prospecting_execution
Revises: 0007_mfa_schema_repair
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_prospecting_execution"
down_revision = "0007_mfa_schema_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("prospecting_jobs")
    }
    if "execution_json" not in columns:
        op.add_column(
            "prospecting_jobs",
            sa.Column(
                "execution_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
        )
        op.alter_column(
            "prospecting_jobs",
            "execution_json",
            server_default=None,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("prospecting_jobs")
    }
    if "execution_json" in columns:
        op.drop_column("prospecting_jobs", "execution_json")
