"""Fila da IA: só a IA trabalha, mesmo com vendedor online (06/11/2026)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.services import conversation_engine
from app.services.queue_routing import AI_WHEN_OFFLINE


class _Db:
    def __init__(self, mode):
        self.mode = mode

    def scalar(self, _stmt):
        return SimpleNamespace(handling_mode=self.mode)


def test_fila_ia_reservada_mesmo_com_vendedor_online(monkeypatch):
    monkeypatch.setattr(
        conversation_engine,
        "settings",
        replace(conversation_engine.settings, ai_sdr_enabled=True, ai_autonomous_enabled=True),
    )
    monkeypatch.setattr(conversation_engine, "human_online_in_queue", lambda db, queue_id: True, raising=False)
    queue = SimpleNamespace(id=1)
    assert conversation_engine._queue_reserved_for_ai(_Db(AI_WHEN_OFFLINE), queue, 10) is True
