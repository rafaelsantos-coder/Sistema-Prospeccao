"""Vendedor IA: CPF primeiro; o nome completo vem do CRM pela consulta cadastral."""

from types import SimpleNamespace

from app.services import ai_seller
from app.services.ai_seller import (
    _adopt_name_from_crm,
    _friendly_plan_label,
    _prompt_for_step,
)


class FakeCRM:
    def __init__(self, opportunity_name=""):
        self.opportunity_name = opportunity_name
        self.lookups = 0

    def get_opportunity(self, opportunity_id):
        self.lookups += 1
        return {"opportunity": {"clientName": self.opportunity_name}}


def _conversation():
    return SimpleNamespace(
        contact=SimpleNamespace(name="Edson"),
        lead=SimpleNamespace(contact_name="Edson"),
    )


def test_codigo_interno_do_plano_vira_texto_de_cliente():
    assert _friendly_plan_label("FTTH_650") == "plano de 650 Mega"
    assert _friendly_plan_label("650 Mega") == "650 Mega"
    assert _friendly_plan_label("") == "plano combinado"


def test_primeira_pergunta_do_fechamento_e_o_cpf_quando_nome_nao_e_conhecido():
    state = SimpleNamespace(step="CPF", plan_label="FTTH_650")
    text = _prompt_for_step(state, {})
    assert "CPF" in text and "nome completo" not in text
    assert "FTTH_650" not in text and "650 Mega" in text


def test_pede_nome_so_depois_da_consulta_quando_o_crm_nao_devolveu():
    state = SimpleNamespace(step="NAME", plan_label="FTTH_650")
    assert "Consulta aprovada" in _prompt_for_step(state, {"cpf": "12345678901"})


def test_nome_completo_vem_da_resposta_da_consulta():
    collected, conversation = {}, _conversation()
    state = SimpleNamespace(opportunity_id=11)
    crm = FakeCRM()
    _adopt_name_from_crm(crm, state, collected, conversation, {"lead": {"clientName": "EDSON LUIZ DA SILVA"}})
    assert collected["name"] == "Edson Luiz Da Silva"
    assert conversation.contact.name == conversation.lead.contact_name == "Edson Luiz Da Silva"
    assert crm.lookups == 0


def test_nome_completo_vem_da_oportunidade_quando_a_consulta_nao_traz():
    collected = {}
    _adopt_name_from_crm(
        FakeCRM("Maria Souza Lima"), SimpleNamespace(opportunity_id=11), collected, _conversation(), {}
    )
    assert collected["name"] == "Maria Souza Lima"


def test_sem_nome_no_crm_nao_inventa_nome():
    collected = {}
    _adopt_name_from_crm(FakeCRM("Edson"), SimpleNamespace(opportunity_id=11), collected, _conversation(), {})
    assert "name" not in collected
    assert ai_seller._looks_like_full_name("Edson") is False
