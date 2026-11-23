"""Regressoes da conversa #5555 e de pedidos de fatura (23/11/2026).

Dados sinteticos, sem numeros, CPF ou historico de outras conversas.
"""
from __future__ import annotations

from app.services.ai_customer_questions import (
    context_denies_city, denies_city, home_wifi_answer, pending_home_wifi_question,
)
from app.services.ai_customer_responses import (
    _current_plan_from_history, priority_reply,
)
from app.services.ai_sdr_catalog_reply import _quote_preference
from app.services.ai_sdr_models import AIDecision
from app.services.ai_sdr_policies import (
    _apply_journey_orchestration_policy, _city_from_sales_context,
    _confirmed_facts_from_history, _known_city_for_fast_path,
)


def _decision(text="Vou te responder agora."):
    return AIDecision(
        reply_text=text,
        qualification_score=50,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        confidence=1.0,
        summary="Teste sintético",
        next_action="Responder a necessidade",
    )


def _context(latest: str, history=None, city="Tuparendi", facts=None):
    return {
        "latest_customer_message": latest,
        "history": history or [],
        "confirmed_facts": facts if facts is not None else {
            "city": city,
            "customer_type": "RESIDENCIAL",
            "product_kind": "INTERNET",
            "existing_customer": "CLIENTE",
            "desired_product": "Fibra",
        },
        "lead": {
            "city": city,
            "confirmed_customer_type": "RESIDENCIAL",
            "confirmed_existing_customer": "CLIENTE",
            "journey": {"offer_plan_code": "FTTH_800", "desired_product": "Internet"},
        },
    }


def test_cliente_pergunta_wifi_e_resposta_eh_tecnica_nao_roteiro():
    context = _context("e funciona em toda a minha casa ?", [
        {"direction": "OUTBOUND", "body": "Temos fibra de 800 Mega por R$ 119,90."},
        {"direction": "INBOUND", "body": "e funciona em toda a minha casa ?"},
    ])
    result = priority_reply(context)
    assert result is not None
    assert "paredes" in result.reply_text
    assert "cômodos" in result.reply_text
    assert "garantir" in result.reply_text
    assert "Ver planos" not in result.reply_text
    assert result.handoff_target == "NONE"


def test_ia_resgata_pergunta_wifi_quando_cliente_cobra_resposta():
    history = [
        {"direction": "INBOUND", "body": "funciona em toda a minha casa?"},
        {"direction": "OUTBOUND", "body": "O que mais pesa na sua decisão?"},
        {"direction": "INBOUND", "body": "quero saber se funciona em toda a casa"},
        {"direction": "OUTBOUND", "body": "Quer ver os planos disponíveis?"},
    ]
    context = _context("mas eu não sou de horizontina e quero que me responda o que pedi", history, city="Horizontina")
    assert pending_home_wifi_question(context) is True
    answer = home_wifi_answer(context)
    assert "Desculpe" in answer
    assert "paredes" in answer
    assert "Horizontina" not in answer
    assert "planos" not in answer


def test_correcao_de_cidade_prevalece_sobre_dado_antigo():
    history = [
        {"direction": "OUTBOUND", "body": "Qual é a sua cidade?"},
        {"direction": "INBOUND", "body": "Horizontina"},
        {"direction": "OUTBOUND", "body": "Em Horizontina, temos 800 Mega."},
        {"direction": "INBOUND", "body": "Mas eu não sou de Horizontina"},
    ]
    facts = _confirmed_facts_from_history(history)
    assert facts["city"] == ""
    context = _context("Mas eu não sou de Horizontina", history, city="Horizontina", facts=facts)
    assert denies_city(context["latest_customer_message"], "Horizontina")
    assert context_denies_city(context, "Horizontina")
    assert _known_city_for_fast_path(context) == ""
    assert _city_from_sales_context(_decision(), context) == ""
    answer = priority_reply(context)
    assert "Em qual cidade" in answer.reply_text
    assert "800 Mega" not in answer.reply_text


def test_nova_cidade_confirmada_corrige_a_anterior():
    history = [
        {"direction": "INBOUND", "body": "não sou de Horizontina"},
        {"direction": "OUTBOUND", "body": "Qual é a cidade da instalação?"},
        {"direction": "INBOUND", "body": "Tuparendi"},
    ]
    context = _context("Tuparendi", history, city="Horizontina", facts={"city": "Tuparendi"})
    assert context_denies_city(context, "Horizontina") is True
    assert _known_city_for_fast_path(context) == "Tuparendi"


def test_pergunta_sobre_dois_roteadores_nao_reabre_catalogo():
    history = [
        {"direction": "OUTBOUND", "body": "Em Tuparendi temos fibra de 800 Mega por R$ 119,90."},
        {"direction": "INBOUND", "body": "e quanto fica com dois roteadores ?"},
    ]
    ctx = _context("e quanto fica com dois roteadores ?", history)
    assert _quote_preference(ctx) is None
    answer = priority_reply(ctx)
    assert "R$ 149,80" in answer.reply_text
    assert "R$ 29,90" in answer.reply_text
    assert "Horizontina" not in answer.reply_text


def test_custo_adicional_sem_cidade_nao_promete_total():
    history = [
        {"direction": "OUTBOUND", "body": "Fibra de 800 Mega por R$ 119,90."},
        {"direction": "INBOUND", "body": "não sou de Horizontina"},
    ]
    ctx = _context(
        "quanto fica com dois roteadores?",
        history, city="Horizontina",
        facts={"city": "Horizontina", "customer_type": "RESIDENCIAL"},
    )
    answer = priority_reply(ctx)
    assert "29,90" in answer.reply_text
    assert "em qual cidade" in answer.reply_text.lower()
    assert "Horizontina" not in answer.reply_text


def test_preco_e_velocidade_do_cliente_sao_extraidos_apenas_das_falas_dele():
    history = [
        {"direction": "OUTBOUND", "body": "Qual a velocidade que você paga hoje?"},
        {"direction": "INBOUND", "body": "800Mb"},
        {"direction": "OUTBOUND", "body": "Para comparar, quanto você paga hoje pela sua conexão?"},
        {"direction": "INBOUND", "body": "99,90"},
        {"direction": "OUTBOUND", "body": "Hoje o Wi-Fi alcança todos os cômodos da sua casa?"},
        {"direction": "INBOUND", "body": "Sim"},
    ]
    assert _current_plan_from_history(_context("quero pagar 99,90", history)) == (800, 99.9, True)
    answer = priority_reply(_context(
        "mas voce ja me passou o plano, esta caro, quero pagar 99,90", history,
    ))
    assert "800 Mega" in answer.reply_text
    assert "650 Mega" in answer.reply_text
    assert "velocidade menor" in answer.reply_text
    assert "segundo roteador" in answer.reply_text
    assert "R$ 99,90" in answer.reply_text
    assert "Telefonia Móvel" not in answer.reply_text
    assert "casa ou empresa" not in answer.reply_text


def test_fatura_para_pagar_nao_pergunta_cidade_ou_produto():
    history = [
        {"direction": "OUTBOUND", "body": "Você já possui algum serviço da Sulnet?"},
        {"direction": "INBOUND", "body": "Não possuo"},
    ]
    context = _context("Quero receber a fatura para poder pagar", history, city="")
    answer = priority_reply(context)
    assert answer.service_redirect is True
    assert answer.followup_needed is False
    assert "3520-8000" in answer.reply_text
    assert "instalação" not in answer.reply_text.lower()
    assert "cidade" not in answer.reply_text.lower()
    assert "CPF" in answer.reply_text


def test_regras_de_estagnacao_nao_trocam_resposta_a_pergunta_direta():
    context = _context("e funciona em toda a minha casa ?", [
        {"direction": "OUTBOUND", "body": "Qual é o plano ideal?"},
        {"direction": "INBOUND", "body": "e funciona em toda a minha casa ?"},
    ], city="")
    context["lead"]["journey"] = {
        "state": "HANDLE_OBJECTION",
        "pending_action": "Resolver objeção",
        "stagnation_count": 5,
    }
    decision = _decision("A cobertura Wi-Fi depende de paredes, distância e posição do roteador.")
    decision.objection = "OTHER"
    result = _apply_journey_orchestration_policy(decision, context)
    assert "cobertura Wi-Fi" in result.reply_text
    assert "o que mais pesa" not in result.reply_text
