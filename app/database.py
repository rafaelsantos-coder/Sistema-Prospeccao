from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine_kwargs: dict = {
    "future": True,
    "pool_pre_ping": True,
}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    sqlite_path = settings.database_url.removeprefix("sqlite:///")
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
else:
    engine_kwargs.update(
        {
            "pool_size": max(1, settings.database_pool_size),
            "max_overflow": max(0, settings.database_max_overflow),
            "pool_timeout": max(1, settings.database_pool_timeout_seconds),
            "pool_recycle": max(60, settings.database_pool_recycle_seconds),
        }
    )

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
