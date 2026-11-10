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


def test_engajado_so_com_resposta_real_nas_ultimas_24h():
    from types import SimpleNamespace

    from app.timeutils import utcnow

    class _Db:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self, _stmt):
            return self.rows

    now = utcnow()
    real = SimpleNamespace(body="Santa Rosa", created_at=now - timedelta(hours=1))
    velha = SimpleNamespace(body="Santa Rosa", created_at=now - timedelta(hours=30))
    robo = SimpleNamespace(body="Estamos fechados, nosso horário de funcionamento é das 8h às 18h", created_at=now - timedelta(hours=1))
    assert ai_followups.customer_engaged(_Db([real]), 1, now)
    assert not ai_followups.customer_engaged(_Db([velha]), 1, now)
    assert not ai_followups.customer_engaged(_Db([robo]), 1, now)
    assert not ai_followups.customer_engaged(_Db([]), 1, now)
