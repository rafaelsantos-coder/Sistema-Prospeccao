from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base
import app.models  # noqa: F401


config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    use_pg_lock = connectable.dialect.name == "postgresql"
    if not use_pg_lock:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        return

    # O advisory lock usa uma conexão AUTOCOMMIT separada. Assim ele serializa
    # os três pre-deploys sem abrir uma transação externa que possa reverter as
    # migrations quando a conexão do lock for fechada.
    with connectable.connect() as raw_lock_connection:
        lock_connection = raw_lock_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        lock_connection.exec_driver_sql(
            "SELECT pg_advisory_lock(7283151042026)"
        )
        try:
            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            lock_connection.exec_driver_sql(
                "SELECT pg_advisory_unlock(7283151042026)"
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
