"""Regressão da SulIA 24/7: compra confirmada não espera horário comercial."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services import ai_followups, ai_seller


def test_hot_followup_cadence_and_24x7_schedule_helpers():
    assert ai_followups.HOT_FOLLOWUP_CADENCE == (
        timedelta(minutes=10),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=12),
    )
    for value in (
        datetime(2026, 11, 21, 0, 12),
        datetime(2026, 11, 21, 23, 40),
        datetime(2026, 11, 22, 11, 0),
    ):
        assert ai_followups.business_window_start(value) == value
        assert ai_followups.closing_window_start(value) == value
    assert ai_followups.MAX_UNANSWERED_HOT_FOLLOWUPS == 4
    assert ai_followups.MAX_UNANSWERED_CLOSING_FOLLOWUPS == 4


def test_closer_cpf_does_not_repeat_the_same_question():
    closing = SimpleNamespace(
        step="CPF",
        plan_label="Internet Fibra 650 Mega",
    )
    collected = {
        "_playbook_step": "CPF",
        "_playbook_question": "Por favor, informe o CPF do titular.",
    }
    texts = [
        ai_seller._seller_followup_prompt(closing, collected, followup_step=step)
        for step in range(1, 5)
    ]
    assert len(set(texts)) == 4
    assert all("CPF" in text or "titular" in text for text in texts)
    assert all("localização" not in text.lower() for text in texts)
    assert all("quer contratar?" not in text.lower() for text in texts)


def test_followup_gates_include_active_human_ownership():
    import inspect
    allowed_source = inspect.getsource(ai_followups._followup_allowed)
    assert "human_is_attending" in allowed_source
    assert "human_stepped_in" in allowed_source
    assert "ATENDIMENTO_HUMANO_ATIVO" in allowed_source
