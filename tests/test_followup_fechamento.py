"""Follow-up do fechamento: cliente que parou na confirmação recebe lembretes (conversa Ricardo, 15/11/2026)."""

from datetime import datetime
from types import SimpleNamespace


def test_janela_do_fechamento_vale_todos_os_dias_das_8_as_21():
    from app.services.ai_followups import closing_window_start

    # Domingo 18h43 (21h43 UTC) + 20 min continua no domingo, dentro da janela.
    assert closing_window_start(datetime(2026, 11, 15, 22, 3)) == datetime(2026, 11, 15, 22, 3)
    # Domingo 21h30 local (00h30 UTC de segunda) vai para segunda 8h local (11h UTC).
    assert closing_window_start(datetime(2026, 11, 16, 0, 30)) == datetime(2026, 11, 16, 11, 0)
    # Segunda 6h local (9h UTC) espera até 8h local.
    assert closing_window_start(datetime(2026, 11, 16, 9, 0)) == datetime(2026, 11, 16, 11, 0)


def test_cadencia_do_fechamento_e_mais_curta_e_tem_tres_lembretes():
    from datetime import timedelta

    from app.services import ai_followups

    assert ai_followups.CLOSING_CADENCE[0] == timedelta(minutes=20)
    assert ai_followups.CLOSING_CADENCE[1] == timedelta(hours=2)
    assert ai_followups.MAX_UNANSWERED_CLOSING_FOLLOWUPS == 3
    assert ai_followups.MAX_UNANSWERED_FOLLOWUPS == 2


def test_lembrete_da_confirmacao_e_curto_e_chama_pelo_nome():
    from app.services.ai_seller import _seller_followup_prompt

    state = SimpleNamespace(step="CONFIRM_SALE", plan_label="Internet Fibra 650 Mega", sign_url="")
    collected = {"name": "RICARDO FEITEN"}
    first = _seller_followup_prompt(state, collected, followup_step=1)
    second = _seller_followup_prompt(state, collected, followup_step=2)
    third = _seller_followup_prompt(state, collected, followup_step=3)
    assert first.startswith("Ricardo, ficou alguma dúvida")
    assert "Confirmo" in first and "assinar pelo celular" in first
    assert "só falta a sua confirmação" in second
    assert "Quero ajustar" in third
    assert len({first, second, third}) == 3


def test_cliente_que_parou_no_fechamento_gera_aviso_para_ligar():
    import inspect

    from app.services import ops_alerts

    assert "check_silent_closings" in inspect.getsource(ops_alerts.run_ops_alert_checks)
