"""O PostgreSQL devolve as colunas de auth_throttles com fuso horário.

Regressão: a partir da 2ª senha errada, record_failure subtraía datetime com
fuso de utcnow() (sem fuso) e o login respondia 500 em vez de "senha inválida".
"""

from __future__ import annotations

from datetime import UTC, timedelta
from types import SimpleNamespace

from app.services import auth_throttle
from app.timeutils import utcnow


class _Db:
    def __init__(self, row):
        self.row = row

    def get(self, _model, _key):
        return self.row

    def add(self, row):
        self.row = row

    def flush(self):
        return None


def _aware(value):
    return value.replace(tzinfo=UTC)


def test_record_failure_accepts_timezone_aware_columns():
    row = SimpleNamespace(
        attempts=1,
        window_started_at=_aware(utcnow() - timedelta(seconds=30)),
        blocked_until=None,
        updated_at=None,
    )

    retry = auth_throttle.record_failure(_Db(row), email="claudia@sulnet.com.br", ip="1.2.3.4")

    assert row.attempts == 2
    assert retry >= 0


def test_record_failure_resets_old_window_with_aware_columns():
    row = SimpleNamespace(
        attempts=4,
        window_started_at=_aware(utcnow() - timedelta(days=1)),
        blocked_until=_aware(utcnow() - timedelta(hours=1)),
        updated_at=None,
    )

    auth_throttle.record_failure(_Db(row), email="claudia@sulnet.com.br", ip="1.2.3.4")

    assert row.attempts == 1


def test_retry_after_seconds_accepts_timezone_aware_block():
    blocked = SimpleNamespace(blocked_until=_aware(utcnow() + timedelta(minutes=5)))
    expired = SimpleNamespace(blocked_until=_aware(utcnow() - timedelta(minutes=5)))

    assert auth_throttle.retry_after_seconds(_Db(blocked), email="a@b.c", ip=None) > 0
    assert auth_throttle.retry_after_seconds(_Db(expired), email="a@b.c", ip=None) == 0
