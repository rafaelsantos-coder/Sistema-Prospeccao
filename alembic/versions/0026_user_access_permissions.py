"""Permissões por usuário, mantendo perfil legado quando NULL.

Revision ID: 0026_user_access_permissions
Revises: 0025_ai_queue_manual_auth
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_user_access_permissions"
down_revision = "0025_ai_queue_manual_auth"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "access_permissions" not in columns:
        op.add_column(
            "users",
            sa.Column("access_permissions", sa.Text(), nullable=True),
        )
    # NULL significa continuar utilizando o perfil anterior. Não migrar
    # usuários existentes para "sem permissões" nem alterar sessões.


def downgrade():
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "access_permissions" in columns:
        op.drop_column("users", "access_permissions")
