"""Vincula e ativa a IA (SDR) nas filas Geral e IA.

Pedido do comercial em 09/11: a fila 1 ficou sem vínculo de IA e a conversa
1156 estava sem atendimento ("ai_job_skipped reason=no_binding").

Revision ID: 0009_bind_ai_queues
Revises: 0008_prospecting_execution
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_bind_ai_queues"
down_revision = "0008_prospecting_execution"
branch_labels = None
depends_on = None

_NAMES = ("geral", "fila geral", "ia", "fila ia")
_CODES = ("GERAL", "FILA-GERAL", "FILA_GERAL", "IA", "FILA-IA", "FILA_IA")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    needed = {"service_queues", "ai_queue_bindings", "ai_agent_profiles"}
    if not needed.issubset(set(inspector.get_table_names())):
        return

    queues = bind.execute(
        sa.text("SELECT id, name, code, team, active FROM service_queues ORDER BY id")
    ).fetchall()
    bindings = {
        row.queue_id: row
        for row in bind.execute(
            sa.text("SELECT id, queue_id, agent_id, handling_mode, active FROM ai_queue_bindings")
        )
    }
    for queue in queues:
        current = bindings.get(queue.id)
        print(
            f"[0009] fila id={queue.id} nome={queue.name!r} codigo={queue.code!r} "
            f"equipe={queue.team} ativa={queue.active} "
            f"vinculo={'nenhum' if not current else (current.handling_mode, current.active, current.agent_id)}"
        )

    agent_id = bind.execute(
        sa.text(
            "SELECT a.id FROM ai_agent_profiles a "
            "LEFT JOIN ai_queue_bindings b ON b.agent_id = a.id "
            "WHERE a.active = :yes AND a.role_type = 'SDR' "
            "GROUP BY a.id ORDER BY COUNT(b.id) DESC, a.id LIMIT 1"
        ),
        {"yes": True},
    ).scalar()
    if not agent_id:
        print("[0009] nenhum agente SDR ativo; nada vinculado")
        return

    for queue in queues:
        name = (queue.name or "").strip().lower()
        code = (queue.code or "").strip().upper()
        if not queue.active or (name not in _NAMES and code not in _CODES):
            continue
        current = bindings.get(queue.id)
        if current:
            bind.execute(
                sa.text(
                    "UPDATE ai_queue_bindings SET active = :yes, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"yes": True, "id": current.id},
            )
            print(f"[0009] fila {queue.id} ({queue.name}): vínculo reativado, modo {current.handling_mode}")
            continue
        mode = "AI_ONLY" if "ia" in name.split() or "IA" in code.replace("_", "-").split("-") else "AI_FIRST"
        bind.execute(
            sa.text(
                "INSERT INTO ai_queue_bindings "
                "(queue_id, agent_id, handling_mode, active, fallback_to_human, created_at, updated_at) "
                "VALUES (:queue_id, :agent_id, :mode, :yes, :no, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"queue_id": queue.id, "agent_id": agent_id, "mode": mode, "yes": True, "no": False},
        )
        print(f"[0009] fila {queue.id} ({queue.name}): vinculada ao agente {agent_id}, modo {mode}")


def downgrade() -> None:
    pass
