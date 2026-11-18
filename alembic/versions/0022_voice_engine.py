"""Real SIP routing configuration and fail-closed voice execution controls."""
from alembic import op
import sqlalchemy as sa

revision = "0022_voice_engine"
down_revision = "0021_voice_sip_tests"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("voice_sip_accounts") as batch:
        batch.add_column(sa.Column("sip_port", sa.Integer(), nullable=False, server_default="5060"))
        batch.add_column(sa.Column("proxy_host", sa.String(190), nullable=False, server_default=""))
        batch.add_column(sa.Column("proxy_port", sa.Integer(), nullable=False, server_default="5060"))
        batch.add_column(sa.Column("dial_domain", sa.String(190), nullable=False, server_default=""))
        batch.add_column(sa.Column("dial_prefix", sa.String(12), nullable=False, server_default=""))
        batch.add_column(sa.Column("strip_brazil_ddi", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("caller_id", sa.String(50), nullable=False, server_default=""))
        batch.add_column(sa.Column("sip_check_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("sip_check_code", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("sip_check_message", sa.String(200), nullable=False, server_default=""))
    with op.batch_alter_table("voice_groups") as batch:
        batch.add_column(sa.Column("transfer_verified_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("voice_campaigns") as batch:
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "voice_dialer_engine",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("backend", sa.String(35), nullable=False, server_default="PJSUA2"),
        sa.Column("last_error", sa.String(200), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "voice_transfer_tests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("voice_sip_accounts.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("voice_groups.id"), nullable=False),
        sa.Column("phone_last4", sa.String(4), nullable=False),
        sa.Column("result", sa.String(35), nullable=False, server_default="PENDENTE"),
        sa.Column("sip_code", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("INSERT INTO voice_dialer_engine (id, enabled, worker_id) VALUES (1, false, '')")


def downgrade():
    op.drop_table("voice_transfer_tests")
    op.drop_table("voice_dialer_engine")
    with op.batch_alter_table("voice_campaigns") as batch:
        for name in ("last_call_at", "stopped_at", "started_at"):
            batch.drop_column(name)
    with op.batch_alter_table("voice_groups") as batch:
        batch.drop_column("transfer_verified_at")
    with op.batch_alter_table("voice_sip_accounts") as batch:
        for name in ("sip_check_message", "sip_check_code", "sip_check_at", "caller_id", "strip_brazil_ddi", "dial_prefix", "dial_domain", "proxy_port", "proxy_host", "sip_port"):
            batch.drop_column(name)
