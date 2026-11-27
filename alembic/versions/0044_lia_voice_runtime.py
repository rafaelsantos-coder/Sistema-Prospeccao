"""Advertise readiness of the Lia Voz worker without exposing credentials."""
from alembic import op
import sqlalchemy as sa

revision = "0044_lia_voice_runtime"
down_revision = "0043_remove_sulnet_wa"
branch_labels = None
depends_on = None


def _has_column():
    # Clean SQLite bootstrap may create current ORM metadata before Alembic
    # runs. Existing PostgreSQL installations still need the additive upgrade.
    columns = sa.inspect(op.get_bind()).get_columns("voice_dialer_engine")
    return any(column["name"] == "lia_runtime_enabled" for column in columns)


def upgrade():
    if not _has_column():
        op.add_column("voice_dialer_engine", sa.Column(
            "lia_runtime_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ))


def downgrade():
    if _has_column():
        op.drop_column("voice_dialer_engine", "lia_runtime_enabled")
