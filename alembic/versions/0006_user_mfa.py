"""MFA TOTP por usuário.

Revision ID: 0006_user_mfa
Revises: 0005_jsonb_companions
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_user_mfa"
down_revision = "0005_jsonb_companions"
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
        op.alter_column("users", "mfa_enabled", server_default=None)
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
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("users")
    }
    if "mfa_secret_encrypted" in columns:
        op.drop_column("users", "mfa_secret_encrypted")
    if "mfa_enabled" in columns:
        op.drop_column("users", "mfa_enabled")
