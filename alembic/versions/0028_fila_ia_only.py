"""Ativa somente a Fila IA autorizada, sem habilitar filas humanas.

Revision ID: 0028_fila_ia_only
Revises: 0027_paced_outbound
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_fila_ia_only"
down_revision = "0027_paced_outbound"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    # Revoke all other queues, including legacy active/manual authorizations.
    db.execute(sa.text("""
        UPDATE ai_queue_bindings
           SET active = false, manual_authorized = false,
               updated_at = CURRENT_TIMESTAMP
         WHERE queue_id NOT IN (
             SELECT id FROM service_queues
              WHERE code = 'FILA-IA' AND active = true
         )
           AND (active = true OR manual_authorized = true)
    """))
    # Select only an active, autonomous SDR. Do not invent or enable an agent.
    db.execute(sa.text("""
        INSERT INTO ai_queue_bindings
            (queue_id, agent_id, handling_mode, active, manual_authorized,
             fallback_to_human, created_at, updated_at)
        SELECT q.id,
               (SELECT a.id FROM ai_agent_profiles a
                 WHERE a.role_type = 'SDR' AND a.active = true
                   AND a.execution_mode = 'AUTO'
                 ORDER BY CASE WHEN a.slug = 'sulia-sdr-digital' THEN 0 ELSE 1 END,
                          a.id
                 LIMIT 1),
               'AI_ONLY', true, true, false,
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
          FROM service_queues q
         WHERE q.code = 'FILA-IA' AND q.active = true
           AND EXISTS (SELECT 1 FROM ai_agent_profiles a
                       WHERE a.role_type = 'SDR'
                         AND a.active = true AND a.execution_mode = 'AUTO')
        ON CONFLICT (queue_id) DO UPDATE
            SET agent_id = EXCLUDED.agent_id,
                handling_mode = 'AI_ONLY',
                active = true,
                manual_authorized = true,
                fallback_to_human = false,
                updated_at = CURRENT_TIMESTAMP
    """))


def downgrade():
    # Fail closed rather than reactivate any previous queue.
    db = op.get_bind()
    db.execute(sa.text("""
        UPDATE ai_queue_bindings SET active = false, manual_authorized = false
         WHERE queue_id IN (SELECT id FROM service_queues WHERE code = 'FILA-IA')
    """))
