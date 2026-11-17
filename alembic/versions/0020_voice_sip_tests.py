"""Single-number SIP test calls; always explicit, audited and rate limited."""
from alembic import op
import sqlalchemy as sa

revision = "0020_voice_sip_tests"
down_revision = "0019_voice_dialer"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("voice_test_calls"):
        op.create_table(
            "voice_test_calls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("voice_sip_accounts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("account_label", sa.String(120), nullable=False),
            sa.Column("target_phone", sa.String(20), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("sip_register_code", sa.Integer(), nullable=True),
            sa.Column("sip_invite_code", sa.Integer(), nullable=True),
            sa.Column("message", sa.String(230), nullable=False),
            sa.Column("initiator_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_voice_test_calls_account_id", "voice_test_calls", ["account_id"])
        op.create_index("ix_voice_test_calls_status", "voice_test_calls", ["status"])
        op.create_index("ix_voice_test_calls_account_created", "voice_test_calls", ["account_id", "created_at"])
    if not sa.inspect(op.get_bind()).has_table("voice_test_agent"):
        op.create_table(
            "voice_test_agent",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("worker_id", sa.String(120), nullable=False),
        )


def downgrade():
    op.drop_table("voice_test_agent")
    op.drop_table("voice_test_calls")
