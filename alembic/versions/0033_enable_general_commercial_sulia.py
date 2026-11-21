"""Habilita SulIA na Geral Comercial por decisao explicita do gestor em 21/11/2026.

Preserva Fila IA e todas as outras filas; nao transfere conversas,
nao repete mensagens antigas nem habilita outras filas automaticamente.

Revision ID: 0033_geral_comercial_sulia
Revises: 0032_repair_dedicated_ai_queue
"""
from __future__ import annotations

import sys
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "0033_geral_comercial_sulia"
down_revision = "0032_repair_dedicated_ai_queue"
branch_labels = None
depends_on = None


def _normalize(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return " ".join(raw.casefold().replace("-", " ").replace("_", " ").split())


def _general_queue(db):
    queues = list(db.execute(sa.text(
        "SELECT id, name, code, active FROM service_queues ORDER BY id"
    )).mappings())
    if not queues:
        return None  # Banco limpo, sem filas cadastradas.

    # O nome operacional e prioritario; nunca ativar outra fila por semelhanca.
    by_name = [
        queue for queue in queues
        if queue["active"] and _normalize(queue["name"]) in ("geral comercial", "comercial geral")
    ]
    by_code = [
        queue for queue in queues
        if queue["active"] and _normalize(queue["code"]) in ("geral", "fila geral", "geral comercial")
    ]
    candidates = by_name if by_name else by_code
    if len(candidates) > 1:
        raise RuntimeError(
            f"Geral Comercial ambigua ({len(candidates)} filas); nenhuma autorizacao concedida."
        )
    if not candidates:
        print("[0033] Geral Comercial nao cadastrada; sem modificacoes", file=sys.stderr, flush=True)
        return None
    return candidates[0]


def upgrade() -> None:
    db = op.get_bind()
    queue = _general_queue(db)
    if queue is None:
        return

    agent_id = db.execute(sa.text(
        "SELECT id FROM ai_agent_profiles WHERE slug = 'sulia-sdr-digital' "
        "AND active = true AND role_type = 'SDR' AND execution_mode = 'AUTO' LIMIT 1"
    )).scalar()
    if agent_id is None:
        agent_id = db.execute(sa.text(
            "SELECT a.id FROM ai_agent_profiles a "
            "JOIN ai_queue_bindings b ON b.agent_id = a.id "
            "JOIN service_queues q ON q.id = b.queue_id "
            "WHERE a.active = true AND a.role_type = 'SDR' "
            "AND a.execution_mode = 'AUTO' AND b.active = true "
            "AND b.manual_authorized = true "
            "ORDER BY CASE WHEN q.name = 'Fila IA' THEN 0 ELSE 1 END, a.id LIMIT 1"
        )).scalar()
    if agent_id is None:
        raise RuntimeError(
            "Geral Comercial nao ativada: perfil SulIA SDR ativo/AUTO ausente."
        )

    queue_id = int(queue["id"])
    params = {"queue": queue_id, "agent": int(agent_id)}
    existing = db.execute(sa.text(
        "SELECT id FROM ai_queue_bindings WHERE queue_id = :queue"
    ), params).scalar()

    if existing is not None:
        db.execute(sa.text(
            "UPDATE ai_queue_bindings SET "
            "agent_id = :agent, handling_mode = 'AI_FIRST', "
            "active = true, manual_authorized = true, fallback_to_human = true, "
            "updated_at = CURRENT_TIMESTAMP WHERE queue_id = :queue"
        ), params)
    else:
        db.execute(sa.text(
            "INSERT INTO ai_queue_bindings "
            "(queue_id, agent_id, handling_mode, active, manual_authorized, "
            "fallback_to_human, created_at, updated_at) "
            "VALUES (:queue, :agent, 'AI_FIRST', true, true, true, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), params)

    authorized = db.execute(sa.text(
        "SELECT COUNT(*) FROM ai_queue_bindings b "
        "JOIN ai_agent_profiles a ON a.id = b.agent_id "
        "WHERE b.queue_id = :queue AND b.active = true "
        "AND b.manual_authorized = true AND b.handling_mode = 'AI_FIRST' "
        "AND a.active = true AND a.role_type = 'SDR' "
        "AND a.execution_mode = 'AUTO'"
    ), params).scalar()
    if int(authorized or 0) != 1:
        raise RuntimeError("Falha ao ativar SulIA na Geral Comercial")

    print(
        f"[0033] SulIA Geral Comercial habilitada queue={queue_id} "
        f"agent={agent_id} mode=AI_FIRST manual_authorized=true, "
        "sem alterar a Fila IA ou conversas existentes",
        file=sys.stderr,
        flush=True,
    )


def downgrade() -> None:
    # Downgrade seguro: apenas desativa a nova vinculacao da Geral Comercial.
    db = op.get_bind()
    queue = _general_queue(db)
    if queue is not None:
        db.execute(sa.text(
            "UPDATE ai_queue_bindings SET active = false, manual_authorized = false, "
            "updated_at = CURRENT_TIMESTAMP WHERE queue_id = :queue"
        ), {"queue": int(queue["id"])})
