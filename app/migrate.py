from __future__ import annotations

import logging
import subprocess

from sqlalchemy import inspect, text

from app.database import engine


LOG = logging.getLogger("sulnet_db_migrate")
_ADVISORY_LOCK_ID = 728419635


def _run_alembic() -> None:
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
    )


def _verify_schema(connection) -> None:
    users = {
        item["name"]
        for item in inspect(connection).get_columns("users")
    }
    required = {
        "session_version",
        "mfa_enabled",
        "mfa_secret_encrypted",
    }
    missing = sorted(required - users)
    if missing:
        raise RuntimeError(
            "Schema incompleto após migration: " + ", ".join(missing)
        )


def main() -> None:
    if engine.dialect.name != "postgresql":
        _run_alembic()
        with engine.connect() as connection:
            _verify_schema(connection)
        return

    with engine.connect() as connection:
        LOG.info("Aguardando lock exclusivo de migration.")
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _ADVISORY_LOCK_ID},
        )
        try:
            LOG.info("Lock de migration adquirido.")
            _run_alembic()
            _verify_schema(connection)
            LOG.info("Migration concluída e schema validado.")
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _ADVISORY_LOCK_ID},
            )


if __name__ == "__main__":
    main()
