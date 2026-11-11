from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import dispatch_queue as dq
from app.timeutils import LOCAL_TZ


def _utc(hour, minute=0, day=10):
    """Instante local de Brasília (hour:minute) convertido para UTC sem fuso."""
    local = datetime(2026, 11, day, hour, minute, tzinfo=LOCAL_TZ)
    return local.astimezone(UTC).replace(tzinfo=None)


def _dispatch(start="09:00", end="18:00"):
    return SimpleNamespace(window_start=start, window_end=end)


def test_validacao_da_janela():
    assert dq.parse_window("", "") == (None, None)
    assert dq.parse_window("09:00", "18:00") == ("09:00", "18:00")
    for start, end in (("09:00", ""), ("", "18:00"), ("9h", "18:00"), ("18:00", "09:00"), ("09:00", "09:00"), ("25:00", "26:00")):
        with pytest.raises(ValueError):
            dq.parse_window(start, end)


def test_dentro_da_janela_envia():
    assert dq.window_next_start(_dispatch(), _utc(10, 30)) is None
    assert dq.window_next_start(_dispatch(), _utc(9, 0)) is None
    assert dq.window_next_start(_dispatch(None, None), _utc(3, 0)) is None


def test_antes_da_janela_espera_abrir_no_mesmo_dia():
    assert dq.window_next_start(_dispatch(), _utc(7, 0)) == _utc(9, 0)


def test_depois_da_janela_espera_o_dia_seguinte():
    assert dq.window_next_start(_dispatch(), _utc(18, 0)) == _utc(9, 0, day=11)
    assert dq.window_next_start(_dispatch(), _utc(22, 30)) == _utc(9, 0, day=11)
