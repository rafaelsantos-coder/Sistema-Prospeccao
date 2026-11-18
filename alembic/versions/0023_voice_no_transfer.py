"""Permite campanhas de áudio somente informativo, sem grupo de ramais.

Revision ID: 0023_voice_no_transfer
Revises: 0022_voice_engine
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_voice_no_transfer"
down_revision = "0022_voice_engine"
branch_labels = None
depends_on = None


def upgrade():
    # Banco existente preserva grupos e campanhas sem modificar historicos.
    # batch_alter_table suporta PostgreSQL e a base SQLite dos testes.
    with op.batch_alter_table("voice_campaigns") as batch_op:
        batch_op.alter_column("group_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    # Em caso de rollback, campanhas sem grupo devem ser encerradas/regularizadas
    # antes de voltar ao schema que exige grupo.
    bind = op.get_bind()
    missing = bind.execute(sa.text(
        "SELECT COUNT(*) FROM voice_campaigns WHERE group_id IS NULL"
    )).scalar() or 0
    if missing:
        raise RuntimeError("Existem campanhas de voz sem grupo; ajuste antes do downgrade.")
    with op.batch_alter_table("voice_campaigns") as batch_op:
        batch_op.alter_column("group_id", existing_type=sa.Integer(), nullable=False)
