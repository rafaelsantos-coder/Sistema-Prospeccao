from __future__ import annotations

from app.config import normalize_database_url


def test_normalize_railway_postgres_url():
    assert (
        normalize_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_preserve_other_database_urls():
    value = "sqlite:///./data/test.db"
    assert normalize_database_url(value) == value
