"""Tabela agregada por território para painel de mapa de calor do crédito.

Revision ID: 0011_credit_geo_aggregates
Revises: 0010_bind_ai_default_queue
"""
from alembic import op
import sqlalchemy as sa

revision = '0011_credit_geo_aggregates'
down_revision = '0010_bind_ai_default_queue'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'credit_map_aggregates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('city', sa.String(120), nullable=False),
        sa.Column('neighborhood', sa.String(180), nullable=False),
        sa.Column('month', sa.String(12), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False),
        sa.Column('signed', sa.Boolean(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.UniqueConstraint('city', 'neighborhood', 'month', 'decision', 'signed', name='uq_credit_map_groups'),
    )
    op.create_index('ix_credit_map_city_month', 'credit_map_aggregates', ['city', 'month'])


def downgrade():
    op.drop_index('ix_credit_map_city_month', table_name='credit_map_aggregates')
    op.drop_table('credit_map_aggregates')
