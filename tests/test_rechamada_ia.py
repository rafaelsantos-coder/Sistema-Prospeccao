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


CATALOGO_CRM = [
    {"id": 9, "name": "Sulnet RESID FIBRA - 650MB - 2-25", "active": True, "categoryId": 76},
    {"id": 11, "name": "Sulnet RESID FIBRA - 800MB - 2-25", "active": True, "categoryId": 76},
    {"id": 20, "name": "CORP - 650M - FTTH", "active": True, "categoryId": 80},
    {"id": 21, "name": "Sulnet RESID FIBRA - 800MB + MOVEL", "active": True, "categoryId": 76},
    {"id": 22, "name": "Sulnet RESID FIBRA - 650MB - 2-24", "active": False, "categoryId": 76},
    {"id": 23, "name": "Sulnet RESID FIBRA - 650MB com 3 mensalidades", "active": True, "categoryId": 76},
]


def test_plano_residencial_vem_do_catalogo_pelo_nome():
    from app.services.ai_seller import _resid_fiber_product, _resid_fiber_speed

    # O código interno do SDR não existe no CRM e travava o fechamento (oportunidades 16278 e 14940).
    assert _resid_fiber_speed("FTTH_650") == 650
    assert _resid_fiber_speed("Fibra 800 Mega") == 800
    assert _resid_fiber_speed("Sulmóvel 10GB") == 0
    assert _resid_fiber_speed("Corp. 650 Mega") == 0
    assert _resid_fiber_product(CATALOGO_CRM, 650)["id"] == 9
    assert _resid_fiber_product(CATALOGO_CRM, 800)["id"] == 11


def test_codigo_ftth_tambem_recebe_a_tag_de_desconto():
    from app.services.ai_seller import PROMO_TAGS, _discount_tag_for

    state = SimpleNamespace(plan_label="FTTH_800", handoff_summary="")
    assert _discount_tag_for(state, {"freeMonths": 3}) == "Desconto 3 mensalidades"
    assert "Promoção Black 2027" in PROMO_TAGS and len(PROMO_TAGS) == 4


def test_rechamada_ampliada_tem_os_modos_e_as_regras_do_gestor():
    import inspect

    from app.services import recall_dispatch

    assert recall_dispatch.WIDE_SELLER_QUIET_DAYS == 3
    assert recall_dispatch._OPEN_DEAL_STAGES == {"PROPOSTA", "NEGOCIACAO"}
    source = inspect.getsource(recall_dispatch.run_recall_from_settings)
    assert "wide_dry_run" in source and "wide_start" in source
    wide = inspect.getsource(recall_dispatch.prepare_wide_recall)
    assert "negociacao_aberta_fica_com_vendedor" in wide
    assert "pediu_para_sair_ou_concluido" in wide
