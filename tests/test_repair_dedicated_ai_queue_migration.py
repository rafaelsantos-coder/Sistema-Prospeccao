"""Regressao da autorizacao: Fila IA real tem nome, mas code diferente."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIAgentProfile, AIQueueBinding, ServiceQueue
from app.services.ai_queue_scope import ai_queue_enabled
from app.services.ai_sdr import queue_ai_binding
from test_ai_all_queues_coverage import _database, _setup


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0032_repair_dedicated_ai_queue.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("repair_dedicated_ai_queue_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_reativa_fila_ia_por_nome_e_revoga_geral(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, general_id, ia_id, _ = _setup(db)
            general = db.get(ServiceQueue, general_id)
            general.name, general.code = "Geral Comercial", "GERAL"
            ia = db.get(ServiceQueue, ia_id)
            ia.name, ia.code = "Fila IA", "IA-PRODUCAO"
            agent = db.get(AIAgentProfile, agent_id)
            agent.active = True
            agent.execution_mode = "AUTO"
            general_binding = db.scalar(
                select(AIQueueBinding).where(AIQueueBinding.queue_id == general_id)
            )
            general_binding.active = True
            general_binding.manual_authorized = True
            general_binding.handling_mode = "AI_FIRST"
            db.commit()

        migration = _module()
        with engine.begin() as connection:
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            migration.upgrade()
            # A segunda execução nao duplica o vínculo.
            migration.upgrade()

        with Session(engine) as db:
            general = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == general_id))
            dedicated = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == ia_id))
            assert general is not None
            assert not general.active and not general.manual_authorized
            assert dedicated is not None
            assert dedicated.active and dedicated.manual_authorized
            assert dedicated.handling_mode == "AI_ONLY"
            assert dedicated.agent_id == agent_id
            assert not ai_queue_enabled(db, general_id)
            assert ai_queue_enabled(db, ia_id)
            assert queue_ai_binding(db, general_id) is None
            assert queue_ai_binding(db, ia_id) is not None
            assert len(list(db.scalars(select(AIQueueBinding)))) == 2
    finally:
        engine.dispose()


def test_migration_falha_fechado_se_houver_duas_filas_ia_ativas(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            _agent_id, first_id, second_id, _third_id = _setup(db)
            for queue_id in (first_id, second_id):
                db.get(ServiceQueue, queue_id).name = "Fila IA"
            db.commit()

        migration = _module()
        with engine.begin() as connection:
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            with pytest.raises(RuntimeError, match="precisa ser unica"):
                migration.upgrade()
    finally:
        engine.dispose()


def test_migration_em_banco_limpo_adiada_sem_erros(monkeypatch):
    engine = _database()
    try:
        migration = _module()
        with engine.begin() as connection:
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            migration.upgrade()
        with Session(engine) as db:
            assert not list(db.scalars(select(AIQueueBinding)))
    finally:
        engine.dispose()
