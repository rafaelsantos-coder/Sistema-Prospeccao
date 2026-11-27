"""Aposentar tabelas do motor WhatsApp experimental sem perder historico.

Revision ID: 0043_remove_sulnet_wa
Revises: 0042_sulnet_wa_gateway

As tabelas so sao excluidas caso estejam vazias; eventual dado historico
permanece para auditoria e nao recebe novas gravacoes pelo app aposentado.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_remove_sulnet_wa"
down_revision = "0042_sulnet_wa_gateway"
branch_labels = None
depends_on = None

_LEGACY = (
    "sulnet_wa_receipts",
    "conversation_sulnet_wa_instances",
    "service_queue_sulnet_wa_instances",
    "sulnet_wa_instances",
)


def upgrade():
    connection = op.get_bind()
    present = set(sa.inspect(connection).get_table_names())
    # Nunca apague automaticamente vinculos de filas, conversas ou recibos
    # que eventualmente tenham sido criados por usuarios antes da aposentadoria.
    for name in _LEGACY:
        if name in present:
            has_rows = connection.execute(
                sa.text(f'SELECT 1 FROM "{name}" LIMIT 1')
            ).scalar_one_or_none()
            if has_rows is not None:
                return
    for name in _LEGACY:
        if name in present:
            op.drop_table(name)


def downgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "sulnet_wa_instances" not in existing:
        op.create_table(
            "sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("session_id", sa.String(80), nullable=False, unique=True),
            sa.Column("phone_label", sa.String(32), nullable=False, server_default=""),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "service_queue_sulnet_wa_instances" not in existing:
        op.create_table(
            "service_queue_sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("queue_id", sa.Integer(), sa.ForeignKey("service_queues.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="RESTRICT"), nullable=False, unique=True),
        )
    if "conversation_sulnet_wa_instances" not in existing:
        op.create_table(
            "conversation_sulnet_wa_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="RESTRICT"), nullable=False),
        )
    if "sulnet_wa_receipts" not in existing:
        op.create_table(
            "sulnet_wa_receipts",
            sa.Column("event_id", sa.String(64), primary_key=True),
            sa.Column("instance_id", sa.Integer(), sa.ForeignKey("sulnet_wa_instances.id", ondelete="CASCADE"), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        )
