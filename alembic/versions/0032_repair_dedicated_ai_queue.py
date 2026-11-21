"""Repara o vínculo da Fila IA real sem autorizar a Geral Comercial.

A migração 0028 procurava exclusivamente code='FILA-IA'. No banco real,
a fila exibida como "Fila IA" pode ter outro code; isso deixou a fila
real sem IA ativa e preservou um vínculo incorreto na fila de entrada.

Revision ID: 0032_repair_dedicated_ai_queue
Revises: 0031_feasibility_map_access
"""
from __future__ import annotations

import sys

import sqlalchemy as sa
from alembic import op

revision = "0032_repair_dedicated_ai_queue"
down_revision = "0031_feasibility_map_access"
branch_labels = None
depends_on = None


def _normal(value: str | None) -> str:
    return " ".join(str(value or "").casefold().replace("-", " ").replace("_", " ").split())

def _dedicated_queue(db):
    queues = list(db.execute(sa.text(
        "SELECT id, name, code, active FROM service_queues ORDER BY id"
    )).mappings())
    if not queues:
        return None  # Instalacao limpa: ainda nao ha filas nem agentes.
    by_name = [q for q in queues if q["active"] and _normal(q["name"]) == "fila ia"]
    # O nome exibido pela equipe prevalece sobre qualquer codigo legado.
    matches = by_name if by_name else [
        q for q in queues if q["active"] and _normal(q["code"]) == "fila ia"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Fila IA precisa ser unica e ativa. "
            f"Encontradas por nome={len(by_name)}, por identificacao={len(matches)}. "
            "Nenhum vinculo sera habilitado ate corrigir o cadastro."
        )
    return matches[0]


def upgrade() -> None:
    db = op.get_bind()
    queue = _dedicated_queue(db)
    if queue is None:
        print("[0032] Banco sem filas: configuracao adiada ate o cadastro da Fila IA", file=sys.stderr)
        return
    queue_id = int(queue["id"])

    agent = db.execute(sa.text(
        "SELECT id FROM ai_agent_profiles "
        "WHERE active = true AND role_type = 'SDR' AND execution_mode = 'AUTO' "
        "ORDER BY CASE WHEN slug = 'sulia-sdr-digital' THEN 0 ELSE 1 END, id LIMIT 1"
    )).scalar()

    if not agent:
        # Restaurar apenas o perfil nomeado de SDR; nunca inventar outro perfil.
        named = db.execute(sa.text(
            "SELECT id FROM ai_agent_profiles "
            "WHERE slug = 'sulia-sdr-digital' AND role_type = 'SDR' LIMIT 1"
        )).scalar()
        if not named:
            raise RuntimeError(
                "Nao ha SulIA SDR autonoma configurada para vincular a Fila IA"
            )
        agent = int(named)
        db.execute(sa.text(
            "UPDATE ai_agent_profiles SET active = true, execution_mode = 'AUTO', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :agent"
        ), {"agent": agent})

    # Nao transferir clientes entre filas; apenas revogar a autorizacao indevida.
    revoked = db.execute(sa.text(
        "UPDATE ai_queue_bindings SET active = false, manual_authorized = false, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE queue_id <> :queue AND (active = true OR manual_authorized = true)"
    ), {"queue": queue_id}).rowcount

    existing = db.execute(sa.text(
        "SELECT id FROM ai_queue_bindings WHERE queue_id = :queue"
    ), {"queue": queue_id}).scalar()
    if existing:
        db.execute(sa.text(
            "UPDATE ai_queue_bindings SET agent_id = :agent, "
            "handling_mode = 'AI_ONLY', active = true, "
            "manual_authorized = true, fallback_to_human = false, "
            "updated_at = CURRENT_TIMESTAMP WHERE queue_id = :queue"
        ), {"queue": queue_id, "agent": int(agent)})
    else:
        db.execute(sa.text(
            "INSERT INTO ai_queue_bindings "
            "(queue_id, agent_id, handling_mode, active, manual_authorized, "
            "fallback_to_human, created_at, updated_at) "
            "VALUES (:queue, :agent, 'AI_ONLY', true, true, false, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"queue": queue_id, "agent": int(agent)})

    # Jobs pendentes em filas nao autorizadas nao podem continuar disparando.
    cancelled_jobs = db.execute(sa.text(
        "UPDATE ai_jobs SET status = 'CANCELADO', locked_at = NULL, "
        "error = 'REPAIR_QUEUE_SCOPE_20261121', updated_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('PENDENTE', 'PROCESSANDO') "
        "AND queue_id <> :queue"
    ), {"queue": queue_id}).rowcount
    cancelled_followups = db.execute(sa.text(
        "UPDATE ai_followup_jobs SET status = 'CANCELADO', locked_at = NULL, "
        "error = 'REPAIR_QUEUE_SCOPE_20261121', updated_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('PENDENTE', 'PROCESSANDO') "
        "AND queue_id <> :queue"
    ), {"queue": queue_id}).rowcount

    other = db.execute(sa.text(
        "SELECT COUNT(*) FROM ai_queue_bindings WHERE queue_id <> :queue "
        "AND (active = true OR manual_authorized = true)"
    ), {"queue": queue_id}).scalar()
    target = db.execute(sa.text(
        "SELECT COUNT(*) FROM ai_queue_bindings "
        "WHERE queue_id = :queue AND active = true AND manual_authorized = true "
        "AND handling_mode = 'AI_ONLY'"
    ), {"queue": queue_id}).scalar()
    if int(other or 0) or int(target or 0) != 1:
        raise RuntimeError("Falha ao reconciliar autorizacao exclusiva da Fila IA")

    print(
        f"[0032] Fila IA real ID={queue_id} code={queue['code']!r} "
        f"agente SDR={agent} autorizado=1 revogados={revoked} "
        f"jobs_cancelados={cancelled_jobs} followups_cancelados={cancelled_followups}",
        file=sys.stderr, flush=True,
    )


def downgrade() -> None:
    # Downgrade nunca reativa autorizacoes legadas.
    db = op.get_bind()
    queue = _dedicated_queue(db)
    if queue is None:
        return
    db.execute(sa.text(
        "UPDATE ai_queue_bindings SET active = false, manual_authorized = false "
        "WHERE queue_id = :queue"
    ), {"queue": int(queue["id"])})
