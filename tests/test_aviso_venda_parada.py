"""Aviso por WhatsApp quando uma venda da IA para no fechamento (pedido do comercial, 15/11/2026)."""

from app.services import ops_alerts


def test_aviso_leva_nome_telefone_e_erro():
    text = ops_alerts.stuck_sale_text(
        name="Maria Silva",
        phone="5555999990000",
        plan="FTTH_650",
        step="WAITING_CRM_READY",
        error="Plano/categoria ainda não foi resolvido no catálogo do CRM.",
        conversation_id=1382,
        opportunity_id=16278,
    )
    assert "Venda parada" in text
    assert "Cliente: Maria Silva" in text
    assert "Telefone: 5555999990000" in text
    assert "Plano: FTTH_650" in text
    assert "catálogo do CRM" in text
    assert "Conversa 1382 · oportunidade 16278" in text


def test_aviso_sem_erro_explica_a_espera_e_mascara_numeros():
    text = ops_alerts.stuck_sale_text(
        name="", phone="", plan="", step="WAITING_CRM_READY", error="", conversation_id=7, opportunity_id=None
    )
    assert "aguardando o CRM" in text
    assert "Cliente: não informado" in text
    masked = ops_alerts.stuck_sale_text(
        name="Ana", phone="55", plan="x", step="CPF", error="CPF 12345678901 recusado", conversation_id=1,
        opportunity_id=None,
    )
    assert "12345678901" not in masked


def test_venda_parada_entra_na_rotina_de_avisos():
    import inspect

    assert "check_stuck_sales" in inspect.getsource(ops_alerts.run_ops_alert_checks)
