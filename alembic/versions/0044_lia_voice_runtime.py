"""Advertise readiness of the Lia Voz worker without exposing credentials."""
from alembic import op
import sqlalchemy as sa

revision = "0044_lia_voice_runtime"
down_revision = "0043_remove_sulnet_wa"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("voice_dialer_engine", sa.Column(
        "lia_runtime_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
    ))


def downgrade():
    op.drop_column("voice_dialer_engine", "lia_runtime_enabled")
