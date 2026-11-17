"""Recorte temporal da recuperação extraordinária autorizada em 16/11/2026.

O mutirão das filas WhatsApp deve considerar somente mensagens que chegaram
a partir de 18h de 16/11/2026 no horário de Brasília (21h UTC). O limite não
altera o fluxo normal de mensagens novas nem dispara contato a clientes antigos.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.timeutils import LOCAL_TZ, utc_naive

UNREAD_RECOVERY_START_LOCAL = datetime(2026, 11, 16, 18, 0, tzinfo=LOCAL_TZ)
UNREAD_RECOVERY_SINCE_UTC = (
    UNREAD_RECOVERY_START_LOCAL.astimezone(timezone.utc).replace(tzinfo=None)
)


def in_unread_recovery_window(timestamp: datetime | None, *, now: datetime) -> bool:
    """Apenas mensagens entre 18h do dia solicitado e o instante da verificação."""
    value = utc_naive(timestamp)
    until = utc_naive(now)
    return bool(value and until and UNREAD_RECOVERY_SINCE_UTC <= value <= until)
