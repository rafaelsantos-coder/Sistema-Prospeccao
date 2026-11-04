"""Correções do teste ao vivo de 04/11 (conversa de teste no número principal)."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from app.config import settings
from app.models import Lead
from app.services import ai_sdr
from app.services.ai_quick_replies import QuickReply, asks_for_free_text, quick_replies_for_message
from app.services.ai_sdr import _confirmed_existing_customer_from_lead, _persist_confirmed_existing_customer


def test_pergunta_de_cidade_nao_leva_botoes():
    buttons = [QuickReply(id="a", label="Quero contratar"), QuickReply(id="b", label="Tenho uma dúvida")]
    assert quick_replies_for_message("Perfeito! Em qual cidade será a instalação?", buttons) == []
    assert quick_replies_for_message("Qual o seu CEP?", []) == []


def test_pergunta_de_escolha_mantem_botoes():
    assert len(quick_replies_for_message("É para sua casa ou empresa?", [])) == 2
    assert not asks_for_free_text("Qual cidade: Santa Rosa ou Ijuí?")
    assert not asks_for_free_text("Você já é cliente Sulnet?")


def test_resposta_ja_e_cliente_vale_so_na_mesma_conversa():
    lead = Lead(phone="5555999999999", raw_data="{}")
    _persist_confirmed_existing_customer(lead, "CLIENTE", conversation_id=375)
    assert _confirmed_existing_customer_from_lead(lead) == "CLIENTE"
    assert _confirmed_existing_customer_from_lead(lead, conversation_id=375) == "CLIENTE"
    assert _confirmed_existing_customer_from_lead(lead, conversation_id=455) == ""
    assert json.loads(lead.raw_data)["ai_existing_customer"]["conversation_id"] == 375


def test_telefone_de_teste(monkeypatch):
    monkeypatch.setattr(ai_sdr, "settings", replace(settings, ai_test_phones=("5555936188241",)))
    assert ai_sdr.is_ai_test_phone("+55 (55) 93618-8241")
    assert ai_sdr.is_ai_test_phone("555593618824 1".replace(" ", ""))
    assert not ai_sdr.is_ai_test_phone("555599676831")
    monkeypatch.setattr(ai_sdr, "settings", replace(settings, ai_test_phones=()))
    assert not ai_sdr.is_ai_test_phone("5555936188241")


def test_vinculo_temporario_so_para_telefone_de_teste(monkeypatch):
    queue = SimpleNamespace(id=1)
    agent = SimpleNamespace(id=7)
    phones = {10: "5555936188241", 11: "555599676831"}

    class _DB:
        def get(self, _model, conversation_id):
            return SimpleNamespace(contact=SimpleNamespace(phone=phones[conversation_id]))

        def scalar(self, _stmt):
            return agent

    monkeypatch.setattr(ai_sdr, "settings", replace(settings, ai_test_phones=("5555936188241",)))
    binding = ai_sdr._test_phone_binding(_DB(), 10, queue)
    assert binding.agent_id == 7 and binding.queue_id == 1 and binding.handling_mode == "AI_FIRST"
    assert ai_sdr._test_phone_binding(_DB(), 11, queue) is None


def test_resumo_de_aceite_e_link_sao_textos_fixos_sem_guardiao():
    from app.services.ai_seller import SALE_ACCEPTANCE_STEP, _is_fixed_closing_text, _sale_acceptance_prompt

    closing = SimpleNamespace(step=SALE_ACCEPTANCE_STEP, sign_url="", plan_label="800 Mega", offer_price=119.9)
    resumo = _sale_acceptance_prompt(closing, {"paymentMethod": "Boleto"})
    lembrete = _sale_acceptance_prompt(closing, {}, reminder=True)
    assert _is_fixed_closing_text(closing, resumo)
    assert _is_fixed_closing_text(closing, lembrete)
    assert not _is_fixed_closing_text(closing, "Qual é o seu CPF?")

    url = "https://app.zapsign.com.br/verificar/abc"
    assinatura = SimpleNamespace(step="WAITING_SIGNATURE", sign_url=url)
    assert _is_fixed_closing_text(assinatura, f"Seu contrato ficou pronto. Você pode assinar por este link: {url}")
    assert not _is_fixed_closing_text(assinatura, "Seu contrato ficou pronto.")
    assert not _is_fixed_closing_text(SimpleNamespace(step="CPF", sign_url=url), url)
