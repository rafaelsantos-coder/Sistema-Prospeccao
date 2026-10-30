"""JSON estruturado pesquisável em JSONB sem quebrar legado.

Revision ID: 0005_jsonb_companions
Revises: 0004_audit_append_only
"""

from alembic import op


revision = "0005_jsonb_companions"
down_revision = "0004_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION safe_jsonb(value text)
        RETURNS jsonb
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        AS $$
        BEGIN
            RETURN value::jsonb;
        EXCEPTION WHEN others THEN
            RETURN '{}'::jsonb;
        END;
        $$;
        """
    )

    specs = [
        ("leads", "raw_data", "raw_data_doc"),
        ("audit_logs", "payload_json", "payload_doc"),
        ("message_events", "payload_json", "payload_doc"),
        ("ai_runs", "output_json", "output_doc"),
        ("ai_closing_states", "collected_json", "collected_doc"),
    ]
    for table, source, target in specs:
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS {target} jsonb
            GENERATED ALWAYS AS (safe_jsonb({source})) STORED;
            """
        )
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS ix_{table}_{target}_gin
            ON {table} USING gin ({target});
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    specs = [
        ("leads", "raw_data_doc"),
        ("audit_logs", "payload_doc"),
        ("message_events", "payload_doc"),
        ("ai_runs", "output_doc"),
        ("ai_closing_states", "collected_doc"),
    ]
    for table, target in specs:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_{target}_gin;")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {target};")
    op.execute("DROP FUNCTION IF EXISTS safe_jsonb(text);")
