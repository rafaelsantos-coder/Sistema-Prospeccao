"""Prospecta VOZ: contas SIP, grupos, campanhas e eventos de chamada."""
from alembic import op
import sqlalchemy as sa

revision = "0019_voice_dialer"
down_revision = "0018_commercial_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "voice_sip_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("domain", sa.String(190), nullable=False),
        sa.Column("username", sa.String(120), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(10), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "voice_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("target_extension", sa.String(30), nullable=False),
        sa.Column("member_extensions", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "voice_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("sip_account_id", sa.Integer(), sa.ForeignKey("voice_sip_accounts.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("voice_groups.id"), nullable=False),
        sa.Column("audio_uri", sa.Text(), nullable=False),
        sa.Column("dtmf_digit", sa.String(1), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(5), nullable=False),
        sa.Column("window_end", sa.String(5), nullable=False),
        sa.Column("legal_basis_verified", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_voice_campaigns_status", "voice_campaigns", ["status"])
    op.create_table(
        "voice_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("voice_campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("phone", sa.String(30), nullable=False),
        sa.Column("status", sa.String(25), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dial_token", sa.String(40), nullable=True, unique=True),
        sa.Column("external_call_id", sa.String(120), nullable=False),
        sa.Column("last_event", sa.String(40), nullable=False),
        sa.Column("last_error", sa.String(200), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "phone", name="uq_voice_campaign_phone"),
    )
    op.create_index("ix_voice_recipients_campaign_id", "voice_recipients", ["campaign_id"])
    op.create_index("ix_voice_recipients_campaign_status", "voice_recipients", ["campaign_id", "status"])
    op.create_index("ix_voice_recipients_dial_token", "voice_recipients", ["dial_token"], unique=True)
    op.create_table(
        "voice_call_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recipient_id", sa.Integer(), sa.ForeignKey("voice_recipients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(80), nullable=False, unique=True),
        sa.Column("event", sa.String(35), nullable=False),
        sa.Column("detail", sa.String(180), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_voice_call_events_recipient_id", "voice_call_events", ["recipient_id"])


def downgrade():
    op.drop_table("voice_call_events")
    op.drop_table("voice_recipients")
    op.drop_table("voice_campaigns")
    op.drop_table("voice_groups")
    op.drop_table("voice_sip_accounts")
