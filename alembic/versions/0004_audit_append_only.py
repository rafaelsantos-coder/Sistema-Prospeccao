"""AuditLog append-only no PostgreSQL.

Revision ID: 0004_audit_append_only
Revises: 0003_money_numeric
"""

from alembic import op


revision = "0004_audit_append_only"
down_revision = "0003_money_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_audit_logs_append_only()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION protect_audit_logs_append_only();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS protect_audit_logs_append_only();")
