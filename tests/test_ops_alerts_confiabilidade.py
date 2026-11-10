"""Avisos de confiabilidade: cliente sem resposta e funil diário da IA (09/11/2026)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import ops_alerts


def test_aviso_de_cliente_sem_resposta_cita_fila_sem_ia(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        ops_alerts,
        "unanswered_customers",
        lambda db: [
            {"conversation_id": 1156, "queue_id": 1, "queue_name": "Loja", "ai_bound": False},
            {"conversation_id": 1599, "queue_id": 1, "queue_name": "Loja", "ai_bound": False},
        ],
    )
    monkeypatch.setattr(
        ops_alerts,
        "send_ops_alert",
        lambda db, *, key, text, **kwargs: sent.update(key=key, text=text) or True,
    )
    assert ops_alerts.check_unanswered_customers(None) == 1
    assert "2 cliente(s) sem resposta" in sent["text"]
    assert "1156" in sent["text"] and "Loja (id 1)" in sent["text"]
    assert sent["key"].startswith("unanswered:")


def test_sem_cliente_esperando_nao_manda_aviso(monkeypatch):
    monkeypatch.setattr(ops_alerts, "unanswered_customers", lambda db: [])
    monkeypatch.setattr(
        ops_alerts, "send_ops_alert", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError)
    )
    assert ops_alerts.check_unanswered_customers(None) == 0


def test_texto_do_funil_diario_traz_conversao():
    text = ops_alerts.daily_funnel_text(
        {
            "attended": 40,
            "replies": 210,
            "closing_started": 6,
            "credit_rejected": 1,
            "contract_sent": 4,
            "won": 3,
            "opt_out": 2,
        },
        "09/11",
    )
    assert "Clientes atendidos: 40" in text and "Vendas assinadas: 3" in text
    assert "7.5%" in text


def test_funil_sem_atendimento_nao_divide_por_zero():
    text = ops_alerts.daily_funnel_text(
        {"attended": 0, "replies": 0, "closing_started": 0, "credit_rejected": 0, "contract_sent": 0, "won": 0, "opt_out": 0},
        "09/11",
    )
    assert "sem base" in text


def test_periodo_aceita_data_sem_fuso():
    start = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    assert ops_alerts._in_period(datetime(2026, 11, 9, 12), start, end)
    assert not ops_alerts._in_period(None, start, end)
