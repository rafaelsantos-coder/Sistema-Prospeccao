"""Ativacao de IA por fila requer autorizacao explicita do gestor.

Revision ID: 0025_ai_queue_manual_auth
Revises: 0024_voice_dial_format

Preserva autorizada apenas a Fila IA dedicada previamente ativa.
Todos os vinculos legados das demais filas ficam sem autorizacao manual.
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_ai_queue_manual_auth"
down_revision = "0024_voice_dial_format"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("ai_queue_bindings")}
    if "manual_authorized" not in columns:
        op.add_column(
            "ai_queue_bindings",
            sa.Column("manual_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    # Nao confundir id=1 legado (Geral Comercial) com a Fila IA.
    # Preservar apenas a Fila IA dedicada se ja estava realmente ativa.
    op.execute(sa.text("""
        UPDATE ai_queue_bindings
           SET manual_authorized = true
         WHERE active = true
           AND queue_id IN (
               SELECT id FROM service_queues
                WHERE code = 'FILA-IA' AND active = true
           )
    """))
    # Desativar de fato todas as demais filas, inclusive vinculos legados.
    # Assim nem rotinas antigas que consultam apenas 'active' poderao atendê-las.
    op.execute(sa.text("""
        UPDATE ai_queue_bindings
           SET active = false,
               manual_authorized = false
         WHERE queue_id NOT IN (
             SELECT id FROM service_queues WHERE code = 'FILA-IA'
         )
    """))


def downgrade():
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("ai_queue_bindings")}
    if "manual_authorized" in columns:
        op.drop_column("ai_queue_bindings", "manual_authorized")
