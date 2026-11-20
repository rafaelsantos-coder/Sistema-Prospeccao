"""Fila persistente para a entrega de respostas da SulIA."""

from alembic import op
import sqlalchemy as sa

from app.models import AIReplyDelivery

revision = "0029_ai_reply_delivery"
down_revision = "0028_fila_ia_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    AIReplyDelivery.__table__.create(bind=bind, checkfirst=True)
    columns = {item["name"] for item in sa.inspect(bind).get_columns("ai_closing_states")}
    for name in ("activation_checked_at", "activation_confirmed_at"):
        if name not in columns:
            op.add_column("ai_closing_states", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    AIReplyDelivery.__table__.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column("ai_closing_states", "activation_confirmed_at")
    op.drop_column("ai_closing_states", "activation_checked_at")
