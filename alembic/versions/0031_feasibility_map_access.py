"""Libera o mapa de viabilidade para os usuários já autorizados a vender.

Revision ID: 0031_feasibility_map_access
Revises: 0030_ai_reply_delivery
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0031_feasibility_map_access'
down_revision = '0030_ai_reply_delivery'
branch_labels = None
depends_on = None

PERMISSION = 'feasibility_map.view'
SALES_ACCESS = (
    ('conversations.view', 'conversations.reply'),
    ('leads.view', 'leads.create'),
    ('conversations.view', 'crm.register'),
)


def _update_permissions(*, add: bool) -> None:
    db = op.get_bind()
    users = sa.table(
        'users',
        sa.column('id', sa.Integer),
        sa.column('role', sa.String),
        sa.column('active', sa.Boolean),
        sa.column('access_permissions', sa.Text),
    )
    statement = sa.select(users.c.id, users.c.access_permissions).where(
        users.c.access_permissions.is_not(None),
    )
    if add:
        statement = statement.where(
            users.c.active.is_(True),
            users.c.role.in_(('CONSULTOR', 'SUPERVISOR', 'GESTOR')),
        )
    for user_id, raw in db.execute(statement).all():
        try:
            codes = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
            continue
        if add and PERMISSION not in codes and any(all(code in codes for code in pair) for pair in SALES_ACCESS):
            codes.append(PERMISSION)
        elif not add and PERMISSION in codes:
            codes = [code for code in codes if code != PERMISSION]
        else:
            continue
        db.execute(
            users.update().where(users.c.id == user_id, users.c.access_permissions == raw)
            .values(access_permissions=json.dumps(codes)),
        )


def upgrade() -> None:
    _update_permissions(add=True)


def downgrade() -> None:
    _update_permissions(add=False)
