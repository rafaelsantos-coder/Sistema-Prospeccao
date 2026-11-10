from __future__ import annotations

from app.routers.zapi import _is_explicit_opt_out
from app.services.ai_quick_replies import infer_quick_replies, quick_replies_for_message
from app.services.conversation_engine import payload_from_me, payload_message_content


def test_infers_casa_empresa_buttons():
    buttons = infer_quick_replies(
        "Boa tarde! Você está buscando internet para sua casa ou empresa?"
    )
    assert buttons == [
        {"id": "para_casa", "label": "Para casa"},
        {"id": "para_empresa", "label": "Para empresa"},
    ]


def test_infers_common_pain_buttons():
    buttons = infer_quick_replies(
        "Hoje, qual é o principal problema com sua internet?"
    )
    assert buttons == [
        {"id": "quedas", "label": "Internet cai"},
        {"id": "lentidao", "label": "Está lenta"},
        {"id": "preco", "label": "Preço alto"},
    ]


def test_explicit_buttons_take_priority_over_inference():
    buttons = quick_replies_for_message(
        "Você quer conhecer a condição?",
        [
            {"id": "quero", "label": "Quero conhecer"},
            {"id": "agora_nao", "label": "Agora não"},
        ],
    )
    assert buttons == [
        {"id": "quero", "label": "Quero conhecer"},
        {"id": "agora_nao", "label": "Agora não"},
    ]


def test_button_response_is_read_as_customer_text():
    message_type, body, media_url, mime_type = payload_message_content(
        {
            "buttonsResponseMessage": {
                "selectedDisplayText": "Para casa",
                "selectedButtonId": "para_casa",
            }
        }
    )
    assert message_type == "TEXT"
    assert body == "Para casa"
    assert media_url is None
    assert mime_type is None



def test_from_me_string_false_is_inbound():
    assert payload_from_me({"fromMe": False}) is False
    assert payload_from_me({"fromMe": "false"}) is False
    assert payload_from_me({"fromMe": "False"}) is False
    assert payload_from_me({"fromMe": "0"}) is False
    assert payload_from_me({}) is False


def test_from_me_string_true_is_outbound():
    assert payload_from_me({"fromMe": True}) is True
    assert payload_from_me({"fromMe": "true"}) is True
    assert payload_from_me({"fromMe": "1"}) is True



def test_polite_decline_is_not_global_opt_out():
    assert _is_explicit_opt_out("Não, obrigado") is False
    assert _is_explicit_opt_out("Não quero saber como acessa o Sulnet Play") is False
    assert _is_explicit_opt_out("Agora não") is False


def test_explicit_contact_stop_is_opt_out():
    assert _is_explicit_opt_out("Não quero mais mensagens") is True
    assert _is_explicit_opt_out("Por favor, não me contate novamente") is True
    assert _is_explicit_opt_out("Me remova da lista") is True


def test_infers_yes_no_buttons_from_explicit_closed_question():
    assert infer_quick_replies(
        "Deseja unificar esta fatura com outras faturas Sulnet? Responda sim ou não."
    ) == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]


def test_portability_number_question_does_not_get_yes_no_buttons():
    # Bug ao vivo 04/11: "deseja portar" virava Sim/Não em vez de pedir o telefone.
    assert infer_quick_replies("Qual número você deseja portar?") == []
    assert quick_replies_for_message("Qual número você deseja portar?", None) == []


def test_infers_buttons_for_core_prospecting_questions():
    assert infer_quick_replies("Você já é cliente Sulnet?") == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]
    assert infer_quick_replies("O que deseja conhecer: Internet ou Telefonia Móvel?") == [
        {"id": "internet", "label": "Internet"},
        {"id": "movel", "label": "Telefonia Móvel"},
    ]
    assert infer_quick_replies(
        "Como cliente Sulnet, deseja Telefonia Móvel ou uma nova conexão para outro endereço?"
    ) == [
        {"id": "movel", "label": "Telefonia Móvel"},
        {"id": "nova_conexao", "label": "Nova conexão"},
    ]


def test_infers_corporate_plan_buttons():
    assert infer_quick_replies(
        "Para empresa temos Corp. 200, Corp. 250 ou Corp. 650. Qual prefere?"
    ) == [
        {"id": "corp_200", "label": "Corp. 200"},
        {"id": "corp_250", "label": "Corp. 250"},
        {"id": "corp_650", "label": "Corp. 650"},
    ]
