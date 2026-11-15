"""Rechamada dos clientes atendidos pela IA: roteiro fixo, fechamento e tag do contrato (15/11/2026)."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import ai_campaign_recall as recall


def test_pergunta_tem_os_tres_botoes():
    assert [item["label"] for item in recall.QUESTION_BUTTONS] == ["Sim", "Agora mesmo", "Não"]
    assert recall.step_of(recall.QUESTION_TEXT) == "QUESTION"


def test_sim_ou_agora_mesmo_leva_a_oferta():
    assert recall.next_action(recall.QUESTION_TEXT, "Sim") == "OFFER"
    assert recall.next_action(recall.QUESTION_TEXT, "Agora mesmo") == "OFFER"


def test_nao_insiste_uma_vez_e_depois_encerra():
    assert recall.next_action(recall.QUESTION_TEXT, "Não") == "INSIST"
    assert recall.step_of(recall.INSIST_TEXT) == "INSIST"
    assert recall.next_action(recall.INSIST_TEXT, "Não") == "END"
    assert recall.next_action(recall.INSIST_TEXT, "Tenho certeza") == "END"
    assert recall.next_action(recall.INSIST_TEXT, "Sim") == "OFFER"


def test_resposta_livre_volta_para_o_fluxo_normal():
    assert recall.next_action(recall.QUESTION_TEXT, "Qual é a promoção?") == ""
    assert recall.next_action(recall.offer_text(), "E para empresa?") == ""
    assert recall.next_action("Você já é cliente Sulnet?", "Sim") == ""


def test_oferta_traz_a_economia_calculada():
    sunday = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    text = recall.offer_text(sunday)
    assert "800 Mega com 3 meses gratuitos" in text
    assert "R$ 359,70" in text and "3 x R$ 119,90" in text
    assert "instalação em até 48 horas" in text
    assert "promoção do nosso domingo" in text
    assert "domingo" not in recall.offer_text(monday)
    assert recall.step_of(text) == "OFFER"
    assert recall.next_action(text, "Sim, vamos fechar") == "CLOSE"
    assert recall.next_action(text, "Agora não") == ""


def test_tag_de_desconto_por_quantidade_de_meses():
    assert recall.discount_tag(1) == "Desconto 1 mensalidade"
    assert recall.discount_tag(2) == "Desconto 2 mensalidades"
    assert recall.discount_tag(3) == "Desconto 3 mensalidades"
    assert recall.discount_tag(0) == ""


def test_fechamento_da_rechamada_leva_tres_meses_gratis():
    from app.services.ai_sdr import _recall_decision

    decision = _recall_decision("CLOSE", "Santa Rosa")
    assert decision.free_months == 3
    assert decision.handoff_target == "CLOSER_AI"
    assert decision.journey_state == "CLOSING"
    assert decision.offer_price == 119.90
    assert recall.has_marker(decision.summary)


def test_mensagens_do_roteiro_nao_transferem():
    from app.services.ai_sdr import _recall_decision

    offer = _recall_decision("OFFER", "")
    assert [item.label for item in offer.quick_replies] == ["Sim, vamos fechar", "Agora não"]
    assert offer.handoff_target == "NONE" and not offer.customer_commitment
    insist = _recall_decision("INSIST", "")
    assert [item.label for item in insist.quick_replies] == ["Sim", "Agora mesmo", "Não"]
    end = _recall_decision("END", "")
    assert end.reply_text == recall.END_TEXT and not end.followup_needed and not end.quick_replies
    city = _recall_decision("ASK_CITY", "")
    assert recall.step_of(city.reply_text) == "CITY"


def test_tag_do_contrato_no_vendedor():
    from app.services import ai_campaign_black as black
    from app.services.ai_seller import _discount_tag_for

    state = SimpleNamespace(plan_label="Fibra 800 Mega", handoff_summary="Rechamada.")
    assert _discount_tag_for(state, {"freeMonths": 3}) == "Desconto 3 mensalidades"
    assert _discount_tag_for(state, {"freeMonths": 0}) == ""
    mobile = SimpleNamespace(plan_label="Sulmóvel 10GB", handoff_summary="")
    assert _discount_tag_for(mobile, {"freeMonths": 3}) == ""
    # A Black tem prioridade no CRM: as duas tags não se misturam.
    black_state = SimpleNamespace(plan_label="Fibra 800 Mega", handoff_summary=black.with_marker("Venda."))
    assert _discount_tag_for(black_state, {"freeMonths": 3}) == ""
