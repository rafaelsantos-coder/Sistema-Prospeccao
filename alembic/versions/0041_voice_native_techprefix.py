"""Numero de origem Native e rodizio SIP opt-in nas campanhas.

Revision ID: 0041_voice_native_techprefix
Revises: 0040_instagram_digital_inbox

As campanhas preexistentes continuam usando a conta SIP e prefixo originais
(ACCOUNT). Nenhuma alteracao de destino, estado, historico ou discagem ativa.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_voice_native_techprefix"
down_revision = "0040_instagram_digital_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    campaigns = {item["name"] for item in inspector.get_columns("voice_campaigns")}
    if "outbound_routing_mode" not in campaigns:
        op.add_column("voice_campaigns", sa.Column(
            "outbound_routing_mode", sa.String(length=20),
            nullable=False, server_default="ACCOUNT",
        ))
    if "native_techprefixes" not in campaigns:
        op.add_column("voice_campaigns", sa.Column(
            "native_techprefixes", sa.String(length=40),
            nullable=False, server_default="",
        ))
    if "native_local_ddd" not in campaigns:
        op.add_column("voice_campaigns", sa.Column(
            "native_local_ddd", sa.String(length=2),
            nullable=False, server_default="55",
        ))

    recipients = {item["name"] for item in inspector.get_columns("voice_recipients")}
    if "outbound_techprefix" not in recipients:
        op.add_column("voice_recipients", sa.Column(
            "outbound_techprefix", sa.String(length=4),
            nullable=False, server_default="",
        ))


def downgrade() -> None:
    bind = op.get_bind()
    recipients = {item["name"] for item in sa.inspect(bind).get_columns("voice_recipients")}
    if "outbound_techprefix" in recipients:
        op.drop_column("voice_recipients", "outbound_techprefix")
    campaigns = {item["name"] for item in sa.inspect(bind).get_columns("voice_campaigns")}
    for col in ("native_local_ddd", "native_techprefixes", "outbound_routing_mode"):
        if col in campaigns:
            op.drop_column("voice_campaigns", col)
