"""Tratamento cooperativo de SIGTERM/SIGINT nos workers singleton.

O encerramento cooperativo somente funciona se o orquestrador der tempo de
draining suficiente. Ele não substitui idempotência nem leases para jobs
externos, tampouco impede SIGKILL durante chamada SIP.
"""
from __future__ import annotations

import logging
import signal
import threading


class GracefulShutdown:
    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._event = threading.Event()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def install(self) -> None:
        """Instala handlers apenas no thread principal do processo worker."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame) -> None:
        if not self._event.is_set():
            self._event.set()
            self._logger.warning(
                "worker_shutdown_requested signal=%s: parar novos claims e finalizar atual",
                signum,
            )

    def wait(self, seconds: float) -> bool:
        """Espera interrompível: True quando foi solicitado encerramento."""
        return self._event.wait(seconds)
