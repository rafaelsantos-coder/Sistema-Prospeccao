"""Regressão: agrupamento semanal do diagnóstico da IA no PostgreSQL 18."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql

from app.models import Message
from app.services.ai_history_audit import _weekly_conversations_query


def test_weekly_aggregate_uses_one_grouping_alias_and_bind_parameter():
    stmt = _weekly_conversations_query(Message.direction == "OUTBOUND")
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "AS week_start" in sql
    assert "GROUP BY week_start" in sql
    assert "ORDER BY week_start" in sql
    assert sql.count("date_trunc(") == 1
    assert list(compiled.params.values()).count("week") == 1


@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true",
    reason="Requer PostgreSQL 18 do job de testes do GitHub Actions",
)
def test_weekly_aggregate_executes_on_postgresql_18():
    """Consulta somente leitura contra o banco descartável do CI."""
    engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/sulnet_ci",
    )
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                _weekly_conversations_query(Message.direction == "__AUDIT_CI_ONLY__")
            ).all()
            assert rows == []
    finally:
        engine.dispose()
