"""Hardening de autenticação.

Revision ID: 0002_auth_security
Revises: 0001_baseline
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_auth_security"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.alter_column(
        "users",
        "session_version",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
