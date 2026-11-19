"""Regressão: agrupamento semanal do diagnóstico da IA no PostgreSQL 18."""

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
