from datetime import timedelta

from app.services import ai_followups


def test_cliente_engajado_tem_primeiro_follow_up_em_30_minutos():
    assert ai_followups.ENGAGED_FOLLOWUP_DELAY == timedelta(minutes=30)
    assert ai_followups.ENGAGED_FOLLOWUP_DELAY < ai_followups.FOLLOWUP_MIN_GAP
    assert ai_followups.ENGAGED_WINDOW == timedelta(hours=24)


def test_prompt_orienta_a_atrair_o_cliente_de_volta():
    source = open(ai_followups.__file__, encoding="utf-8").read()
    assert "COMO ATRAIR O CLIENTE DE VOLTA" in source
    assert "customer_engaged" in source
