"""Shared SIP execution slot for the native engine and legacy diagnostic agent.

A PostgreSQL session advisory lock spans claim, registration, INVITE and
cleanup. It is independent of row transactions and survives their commits.
All internal SIP workers must use this slot before enabling shared Lia Voz.
External devices or another database are outside its scope.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import time

from sqlalchemy import text

from app.database import engine

# App-specific two-int namespace; do not reuse for unrelated locks.
_LOCK_NAMESPACE = 826040
_LOCK_RESOURCE = 1804
_LOCAL = Lock()
_CURRENT = ContextVar("voice_execution_slot", default=None)


class ExecutionSlot:
    def __init__(self, conn=None, pid=None):
        self.conn = conn
        self.pid = pid
        self.last_check = 0.0
        self.valid = True

    def healthy(self):
        if self.conn is None:
            return self.valid
        if not self.valid:
            return False
        now = time.monotonic()
        if now - self.last_check < 1:
            return True
        try:
            # A lost or reconnected database session no longer owns the lock.
            current = self.conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self.conn.commit()
            self.valid = current == self.pid
            self.last_check = now
        except Exception:
            self.valid = False
        return self.valid


def slot_active():
    return _CURRENT.get() is not None


def slot_healthy(*, required=False):
    slot = _CURRENT.get()
    return slot.healthy() if slot is not None else not required


@contextmanager
def voice_execution_slot():
    """Yield True only when this worker owns the single execution slot."""
    if engine.dialect.name == "sqlite":
        # Offline tests/development only; not a distributed production lock.
        acquired = _LOCAL.acquire(blocking=False)
        if not acquired:
            yield False
            return
        token = _CURRENT.set(ExecutionSlot())
        try:
            yield True
        finally:
            _CURRENT.reset(token)
            _LOCAL.release()
        return
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Voice execution requires PostgreSQL or local SQLite")
    params = {"namespace": _LOCK_NAMESPACE, "resource": _LOCK_RESOURCE}
    with engine.connect() as conn:
        acquired = False
        token = None
        try:
            row = conn.execute(text(
                "SELECT pg_try_advisory_lock(:namespace, :resource), pg_backend_pid()"
            ), params).one()
            acquired = bool(row[0])
            conn.commit()
            if acquired:
                token = _CURRENT.set(ExecutionSlot(conn, row[1]))
            yield acquired
        finally:
            if token is not None:
                _CURRENT.reset(token)
            if acquired:
                try:
                    if conn.in_transaction():
                        conn.rollback()
                    unlocked = conn.execute(text(
                        "SELECT pg_advisory_unlock(:namespace, :resource)"
                    ), params).scalar_one()
                    conn.commit()
                    if not unlocked:
                        conn.invalidate()
                except Exception:
                    # Do not return a potentially locked session to the pool.
                    conn.invalidate()
