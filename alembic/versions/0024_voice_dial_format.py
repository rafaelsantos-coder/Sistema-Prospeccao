"""Normaliza o plano de discagem SIP de campanhas em producao.

Revision ID: 0024_voice_dial_format
Revises: 0023_voice_no_transfer

Todas as campanhas existentes passam a discar DDD + telefone, sem adicionar DDI.
Campanhas futuras podem optar explicitamente por E.164 quando o PABX exigir.
O numero canonico do contato no CRM e a blacklist nao sao modificados.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_voice_dial_format"
down_revision = "0023_voice_no_transfer"
branch_labels = None
depends_on = None


def upgrade():
    # Baseline de instalacoes novas gera as tabelas com o modelo atual;
    # bancos de producao existentes ainda nao possuem esta coluna.
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("voice_campaigns")}
    if "dial_format" not in columns:
        op.add_column("voice_campaigns", sa.Column(
            "dial_format", sa.String(length=12), nullable=False, server_default="NATIONAL"
        ))


def downgrade():
    if "dial_format" in {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("voice_campaigns")
    }:
        op.drop_column("voice_campaigns", "dial_format")
