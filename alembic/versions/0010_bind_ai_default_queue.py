"""Vincula a IA também na fila padrão de entrada e lista as filas no log.

A 0009 procurou as filas pelos nomes "Geral" e "IA", mas a fila 1 (equipe LOJA)
continuou sem vínculo: o nome dela no banco é outro. Esta migration cobre a
fila padrão de entrada e nomes que contenham "geral", e escreve a lista de
filas em stderr (o stdout do pré-deploy não aparece no log do Railway).

Revision ID: 0010_bind_ai_default_queue
Revises: 0009_bind_ai_queues
"""

import sys
import unicodedata

from alembic import op
import sqlalchemy as sa


revision = "0010_bind_ai_default_queue"
down_revision = "0009_bind_ai_queues"
branch_labels = None
depends_on = None


def _log(*parts) -> None:
    print("[0010]", *parts, file=sys.stderr, flush=True)


def _fold(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return text.strip().lower()


def _is_target(queue) -> bool:
    name = _fold(queue.name)
    code = _fold(queue.code).replace("_", "-")
    words = set(name.replace("-", " ").split()) | set(code.split("-"))
    return bool(queue.is_default_inbound) or "geral" in words or "ia" in words


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"service_queues", "ai_queue_bindings", "ai_agent_profiles"}.issubset(tables):
        return

    queues = bind.execute(
        sa.text(
            "SELECT id, name, code, team, active, is_default_inbound "
            "FROM service_queues ORDER BY id"
        )
    ).fetchall()
    bindings = {
        row.queue_id: row
        for row in bind.execute(
            sa.text("SELECT id, queue_id, agent_id, handling_mode, active FROM ai_queue_bindings")
        )
    }
    agent_id = bind.execute(
        sa.text(
            "SELECT a.id FROM ai_agent_profiles a "
            "LEFT JOIN ai_queue_bindings b ON b.agent_id = a.id "
            "WHERE a.active = :yes AND a.role_type = 'SDR' "
            "GROUP BY a.id ORDER BY COUNT(b.id) DESC, a.id LIMIT 1"
        ),
        {"yes": True},
    ).scalar()
    _log("agente SDR ativo:", agent_id)

    for queue in queues:
        current = bindings.get(queue.id)
        _log(
            "fila", queue.id, repr(queue.name), repr(queue.code), queue.team,
            "ativa" if queue.active else "inativa",
            "padrao" if queue.is_default_inbound else "-",
            "vinculo:", (current.handling_mode, bool(current.active), current.agent_id) if current else None,
        )
        if not queue.active or not agent_id or not _is_target(queue):
            continue
        if current:
            if not current.active:
                bind.execute(
                    sa.text(
                        "UPDATE ai_queue_bindings SET active = :yes, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ),
                    {"yes": True, "id": current.id},
                )
                _log("fila", queue.id, "vinculo reativado, modo", current.handling_mode)
            continue
        mode = "AI_ONLY" if "ia" in _fold(queue.name).split() else "AI_FIRST"
        bind.execute(
            sa.text(
                "INSERT INTO ai_queue_bindings "
                "(queue_id, agent_id, handling_mode, active, fallback_to_human, created_at, updated_at) "
                "VALUES (:queue_id, :agent_id, :mode, :yes, :no, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"queue_id": queue.id, "agent_id": agent_id, "mode": mode, "yes": True, "no": False},
        )
        _log("fila", queue.id, "vinculada ao agente", agent_id, "modo", mode)


def downgrade() -> None:
    pass
