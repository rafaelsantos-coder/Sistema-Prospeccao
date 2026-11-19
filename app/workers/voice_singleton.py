"""Singleton de discagem PJSUA2 baseado em lock de sessão do PostgreSQL.

Sem migração. A conexão que sustenta pg_advisory_lock permanece aberta por
toda a vida do worker. O código deliberadamente falha fechado em bancos sem
suporte ao lock ou na perda da sessão, impedindo múltiplos motores independentes.
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
# Chave global fixa "SULNETV1" (bigint positivo); não compartilha com a fila de WhatsApp.
VOICE_SINGLETON_LOCK_KEY = 0x53554C4E45545631


class VoiceLeadershipLost(RuntimeError):
    """Um worker não pode reservar ou iniciar chamadas sem liderança exclusiva."""


class VoiceLeaderLease:
    def __init__(self, connection: Connection, backend_pid: int):
        self._connection = connection
        self._backend_pid = backend_pid
        self._closed = False
        self._last_check = 0.0

    def assert_held(self, *, force: bool = False) -> None:
        """Detecta falha/desconexão sem aceitar reconexão silenciosa sem lock."""
        if self._closed or self._connection.closed or self._connection.invalidated:
            raise VoiceLeadershipLost("Conexao do lock SIP foi perdida.")
        now = time.monotonic()
        if not force and now - self._last_check < 1.0:
            return
        try:
            backend_pid = int(
                self._connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            self._connection.commit()
        except Exception as exc:
            self._closed = True
            raise VoiceLeadershipLost("Nao foi possivel confirmar exclusividade SIP.") from exc
        if backend_pid != self._backend_pid:
            self._closed = True
            raise VoiceLeadershipLost("Sessao PostgreSQL foi trocada; lock SIP invalido.")
        self._last_check = now

    def healthy(self) -> bool:
        """Usado pelo callback do motor nativo para abortar em perda do lock."""
        try:
            self.assert_held()
            return True
        except VoiceLeadershipLost:
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
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": VOICE_SINGLETON_LOCK_KEY},
            ).scalar_one()
            self._connection.commit()
            if unlocked is not True:
                raise RuntimeError("PostgreSQL nao confirmou liberacao do lock.")
        except Exception:
            LOG.warning("Falha ao liberar lock global SIP; descartando conexao do pool.", exc_info=True)
            try:
                self._connection.invalidate()
            except Exception:
                LOG.exception("Falha ao invalidar conexao SIP.")
        finally:
            self._closed = True
            self._connection.close()


@contextmanager
def voice_engine_leadership() -> Iterator[VoiceLeaderLease | None]:
    """Obtém liderança sem bloqueio; segundas réplicas permanecem em standby."""
    if engine.dialect.name != "postgresql":
        raise VoiceLeadershipLost("Discagem segura exige PostgreSQL para lock singleton.")
    connection = engine.connect()
    lease: VoiceLeaderLease | None = None
    safe_to_pool = False
    try:
        row = connection.execute(
            text(
                "SELECT pg_try_advisory_lock(:lock_key) AS acquired, "
                "pg_backend_pid() AS backend_pid"
            ),
            {"lock_key": VOICE_SINGLETON_LOCK_KEY},
        ).one()
        # Instanciar o lease ANTES de commit: se commit falhar apos o servidor
        # adquirir o lock, o finally descarta/libera a sessao corretamente.
        if row.acquired:
            lease = VoiceLeaderLease(connection, int(row.backend_pid))
        connection.commit()
        if lease is not None:
            lease.assert_held(force=True)
        safe_to_pool = True
        yield lease
    finally:
        if lease is not None:
            lease.release()
        else:
            # O banco pode ter adquirido o lock sem que o cliente recebesse
            # a resposta. Uma aquisicao incompleta nao pode voltar ao pool.
            try:
                if not safe_to_pool and not connection.closed:
                    connection.invalidate()
            finally:
                connection.close()
