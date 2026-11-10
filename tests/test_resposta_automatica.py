"""Resposta automática de empresa e limite de follow-up (conversas de 10/11/2026)."""

from app.services.auto_reply import is_business_auto_reply
from app.services import ai_followups


def test_reconhece_respostas_automaticas_reais():
    assert is_business_auto_reply(
        "Olá! Estamos fechados no momento! Horários de funcionamento: Segunda a Sexta - 16:00 às 23:00"
    )
    assert is_business_auto_reply(
        "Olá, tudo bem? Aqui é o Adrian assistente do Dr Thales. Que bom receber você aqui na "
        "Oral Sin Implantes Santa Rosa. Como podemos te ajudar?"
    )
    assert is_business_auto_reply(
        "A Fronza Odontologia agradece o seu contato. No momento, não estamos disponíveis."
    )


def test_cliente_de_verdade_nao_e_resposta_automatica():
    for text in (
        "Não",
        "Agora não",
        "Estou com problema na internet",
        "Vcs tem loja aqui",
        "119 da internet mais o valor do móvel?",
        "Como podemos fazer para contratar?",
        "Quero ver plano para minha empresa",
    ):
        assert not is_business_auto_reply(text), text


def test_limite_de_follow_up_sem_resposta():
    assert ai_followups.MAX_UNANSWERED_FOLLOWUPS == 3
