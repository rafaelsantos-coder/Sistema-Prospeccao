"""Follow-up do fechamento: cliente que parou na confirmação recebe lembretes (conversa Ricardo, 15/11/2026)."""

from datetime import datetime
from types import SimpleNamespace


def test_fechamento_funciona_24_horas_todos_os_dias():
    from app.services.ai_followups import closing_window_start

    # Domingo 18h43 (21h43 UTC) + 20 min continua no domingo, dentro da janela.
    assert closing_window_start(datetime(2026, 11, 15, 22, 3)) == datetime(2026, 11, 15, 22, 3)
    # A madrugada não adia nenhuma retomada do fechamento.
    assert closing_window_start(datetime(2026, 11, 16, 0, 30)) == datetime(2026, 11, 16, 0, 30)
    assert closing_window_start(datetime(2026, 11, 16, 9, 0)) == datetime(2026, 11, 16, 9, 0)


def test_cadencia_do_fechamento_e_mais_curta_e_tem_tres_lembretes():
    from datetime import timedelta

    from app.services import ai_followups

    assert ai_followups.CLOSING_CADENCE[0] == timedelta(minutes=10)
    assert ai_followups.CLOSING_CADENCE[1] == timedelta(minutes=30)
    assert ai_followups.CLOSING_CADENCE[2] == timedelta(hours=2)
    assert ai_followups.CLOSING_CADENCE[3] == timedelta(hours=12)
    assert ai_followups.MAX_UNANSWERED_CLOSING_FOLLOWUPS == 4
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


def test_lembrete_da_confirmacao_e_texto_fixo_e_nao_passa_pelo_guardiao():
    from app.services.ai_seller import _confirm_sale_followup_text, _is_fixed_closing_text

    state = SimpleNamespace(
        step="CONFIRM_SALE",
        plan_label="Internet Fibra 650 Mega",
        sign_url="",
        contract_status="",
        collected_json='{"name": "Ricardo Feiten"}',
    )
    for step in (1, 2, 3):
        text = _confirm_sale_followup_text(state, {"name": "Ricardo Feiten"}, step)
        assert _is_fixed_closing_text(state, text)
    assert not _is_fixed_closing_text(state, "Texto livre gerado pelo modelo.")
