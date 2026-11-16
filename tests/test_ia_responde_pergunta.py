"""Conversa 1679 (16/11/2026): com o Sulmóvel já oferecido, a IA não volta ao roteiro de entrada."""

from app.services.ai_sdr_policies import (
    _apply_prospecting_entry_policy,
    _customer_asked_question,
    _product_already_in_talk,
)
from tests.test_ai_sdr import _base_prospecting_entry_decision


def _context(latest, journey=None, history=None):
    return {
        "latest_customer_message": latest,
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "", "journey": journey or {}},
        "history": history or [{"direction": "INBOUND", "body": latest}],
    }


def test_detecta_pergunta_do_cliente():
    assert _customer_asked_question("Quais os doc. que eu preciso ter!")
    assert _customer_asked_question("Ele pega em todo o estado.")
    assert _customer_asked_question("?")
    assert not _customer_asked_question("Não")
    assert not _customer_asked_question("Sim")


def test_produto_em_conversa_vem_da_memoria_e_nao_da_proposta_nova():
    assert _product_already_in_talk(_context("oi", {"offer_plan_code": "SULMOVEL_5GB"})) == "MOBILE"
    assert _product_already_in_talk(_context("oi")) == ""


def test_pergunta_com_produto_em_conversa_mantem_a_resposta_do_modelo():
    decision = _base_prospecting_entry_decision()
    decision.reply_text = "Sim! O Sulmóvel tem área de cobertura nacional, então funciona em todo o estado."
    result = _apply_prospecting_entry_policy(
        decision, _context("Ele pega em todo o estado.", {"offer_plan_code": "SULMOVEL_5GB"})
    )
    assert "cobertura nacional" in result.reply_text


def test_primeiro_contato_continua_perguntando_se_ja_e_cliente():
    decision = _base_prospecting_entry_decision()
    result = _apply_prospecting_entry_policy(decision, _context("Quanto custa a internet?"))
    assert "já é cliente" in result.reply_text


def test_nao_cliente_com_produto_em_conversa_nao_volta_para_escolha_de_produto():
    decision = _base_prospecting_entry_decision()
    decision.reply_text = "Perfeito! Então seguimos com o Sulmóvel 5GB. Quer contratar com portabilidade?"
    history = [
        {"direction": "OUTBOUND", "body": "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"},
        {"direction": "INBOUND", "body": "Não"},
    ]
    result = _apply_prospecting_entry_policy(
        decision, _context("Não", {"offer_plan_code": "SULMOVEL_5GB"}, history)
    )
    assert "Sulmóvel 5GB" in result.reply_text
