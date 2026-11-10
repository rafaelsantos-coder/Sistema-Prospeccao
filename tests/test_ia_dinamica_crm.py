from types import SimpleNamespace

from app.services import ai_sdr_policy, crm_notes


def _decision(**kw):
    q = SimpleNamespace(
        current_provider="Provedor X", current_speed="", current_price="R$ 99",
        city="Santo Ângelo", customer_type="EMPRESA",
    )
    base = dict(
        objection="PRICE", qualification=q, offer_plan_code="FTTH_650", free_months=1,
        proposal_summary="650 Mega por R$ 99,90", next_action="Confirmar endereço",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_nota_do_sdr_resume_objecao_e_oferta_sem_dados_pessoais():
    result, text = crm_notes.sdr_note_text(_decision())
    assert result == "Objeção e oferta"
    for trecho in ("preço", "R$ 99", "FTTH_650", "1 mês", "Santo Ângelo", "Confirmar endereço"):
        assert trecho in text


def test_sem_nada_relevante_nao_gera_nota():
    q = SimpleNamespace(current_provider="", current_speed="", current_price="", city="", customer_type="")
    d = _decision(objection="NONE", qualification=q, offer_plan_code="", free_months=0, proposal_summary="")
    assert crm_notes.sdr_note_text(d) is None


def test_credito_aprovado_vira_marco():
    state = SimpleNamespace(
        opportunity_id=1, step="WAITING_CONTRACT", plan_label="", offer_price=None,
        credit_status="Aprovado", collected_json="{}",
    )
    keys = [n[0] for n in crm_notes.pending_notes(state, {})]
    assert "credit_approved" in keys and "contract_sent" in keys


def test_prompt_traz_metodo_de_venda_dinamica():
    source = open(ai_sdr_policy.__file__, encoding="utf-8").read()
    assert "VENDA DINÂMICA" in source and "UM dado por vez" in source and "Sulnet Play" in source
