"""Bloqueio de conta SIP compartilhado entre discador e agente de testes.

Cada conta possui seu lock PostgreSQL (par de inteiros de 32 bits). Isso
impede dois processos independentes de iniciar SIP na mesma conta ao mesmo
tempo sem bloquear as outras contas.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import engine


LOG = logging.getLogger(__name__)
# Namespace de advisory locks por conta "SIP1" (distinto do lock global bigint).
SIP_ACCOUNT_LOCK_NAMESPACE = 0x53495031


class SIPAccountLockLost(RuntimeError):
    """Não há permissão exclusiva para usar a conta SIP."""


class SIPAccountLease:
    def __init__(self, connection: Connection, backend_pid: int, account_id: int):
        self._connection = connection
        self._backend_pid = backend_pid
        self._account_id = account_id
        self._last_check = 0.0
        self._closed = False

    def assert_held(self, *, force: bool = False) -> None:
        if self._closed or self._connection.closed or self._connection.invalidated:
            raise SIPAccountLockLost("Conexao do lock de conta SIP foi perdida.")
        now = time.monotonic()
        if not force and now - self._last_check < 1.0:
            return
        try:
            current_pid = int(
                self._connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            self._connection.commit()
        except Exception as exc:
            self._closed = True
            raise SIPAccountLockLost("Nao foi possivel verificar exclusividade SIP.") from exc
        if current_pid != self._backend_pid:
            self._closed = True
            raise SIPAccountLockLost("Sessao da conta SIP foi trocada.")
        self._last_check = now

    def healthy(self) -> bool:
        try:
            self.assert_held()
            return True
        except SIPAccountLockLost:
            return False

    def release(self) -> None:
        """Nunca devolver ao pool uma sessao que possa reter lock advisory."""
        if self._connection.closed:
            self._closed = True
            return
        try:
            if self._closed or self._connection.invalidated:
                # Falha anterior: nao sabemos se o lock segue vivo no servidor.
                self._connection.invalidate()
                return
            current_pid = int(
                self._connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            if current_pid != self._backend_pid:
                raise RuntimeError("Sessao PostgreSQL mudou antes do unlock.")
            unlocked = self._connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :account_id)"),
                {"namespace": SIP_ACCOUNT_LOCK_NAMESPACE, "account_id": self._account_id},
            ).scalar_one()
            self._connection.commit()
            if unlocked is not True:
                raise RuntimeError("PostgreSQL nao confirmou liberacao do lock.")
        except Exception:
            LOG.warning("Falha ao liberar lock da conta SIP; descartando conexao do pool.", exc_info=True)
            try:
                self._connection.invalidate()
            except Exception:
                LOG.exception("Falha ao invalidar conexao SIP.")
        finally:
            self._closed = True
            self._connection.close()


@contextmanager
def sip_account_lock(account_id: int) -> Iterator[SIPAccountLease | None]:
    """Obtém o lock sem bloquear; retorna None quando a conta está ocupada."""
    if engine.dialect.name != "postgresql":
        raise SIPAccountLockLost("Conta SIP requer PostgreSQL para exclusividade.")
    if not isinstance(account_id, int) or not (0 < account_id < 2**31):
        raise SIPAccountLockLost("ID de conta SIP invalido para o lock.")
    connection = engine.connect()
    lease: SIPAccountLease | None = None
    safe_to_pool = False
    try:
        row = connection.execute(
            text(
                "SELECT pg_try_advisory_lock(:namespace, :account_id) AS acquired, "
                "pg_backend_pid() AS backend_pid"
            ),
            {"namespace": SIP_ACCOUNT_LOCK_NAMESPACE, "account_id": account_id},
        ).one()
        # Instanciar o lease ANTES de commit pelo mesmo motivo de segurança.
        if row.acquired:
            lease = SIPAccountLease(connection, int(row.backend_pid), account_id)
        connection.commit()
        if lease is not None:
            lease.assert_held(force=True)
        safe_to_pool = True
        yield lease
    finally:
        if lease is not None:
            lease.release()
        else:
            try:
                if not safe_to_pool and not connection.closed:
                    connection.invalidate()
            finally:
                connection.close()
