"""Repara colunas MFA em bancos que sofreram migration concorrente.

Revision ID: 0007_mfa_schema_repair
Revises: 0006_user_mfa
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_mfa_schema_repair"
down_revision = "0006_user_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("users")
    }

    if "mfa_enabled" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "mfa_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.alter_column(
            "users",
            "mfa_enabled",
            server_default=None,
        )

    if "mfa_secret_encrypted" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "mfa_secret_encrypted",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
        )
        op.alter_column(
            "users",
            "mfa_secret_encrypted",
            server_default=None,
        )


def downgrade() -> None:
    # Migration reparadora: não remove colunas funcionais num rollback.
    pass
