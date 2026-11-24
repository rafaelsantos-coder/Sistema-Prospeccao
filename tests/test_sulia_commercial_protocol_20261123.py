"""16 cenarios de aceite extraidos do protocolo comercial de 23/11/2026.

Nao usa dados pessoais do export e nao tenta inferir sucesso de CRM ou assinatura.
Teste unitario do protocolo; homologacao de negocio / WhatsApp e separada.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.ai_sales_protocol import (
    BASELINE_RULES, BRAZIL, OBSERVABLE_STAGES, SalesEvidence,
    classify_signal, coaching_for_context, requested_return_not_before,
    stage_from_evidence,
)


def _br(year, month, day, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BRAZIL)


def _local(utc_value):
    return utc_value.astimezone(ZoneInfo("America/Sao_Paulo"))


# 01. Compra imediata: sem reapresentacao generica
def test_01_quero_contratar_avanca():
    assert classify_signal("Quero contratar hoje!") == "PURCHASE_INTENT"
    assert "nao reapresente catalogo" in coaching_for_context(
        {"latest_customer_message": "Quero contratar hoje"}
    )


# 02. Preco condicionado: sem desconto inventado
def test_02_teto_de_preco_nao_vira_desconto():
    assert classify_signal("Até R$ 90 eu fecho") == "CONDITIONAL_BUDGET"
    assert "nao invente desconto" in coaching_for_context(
        {"latest_customer_message": "Até R$ 90 eu fecho"}
    )


# 03. Segundo decisor: seguir horario escolhido
def test_03_responder_segunda_respeita_decisor():
    message = "Vou perguntar ao meu esposo e respondo segunda"
    assert classify_signal(message) == "DECISION_MAKER"
    # Domingo 22/11, 10h -> segunda 23/11, 15h (padrao documentado)
    deadline = requested_return_not_before(message, _br(2026, 11, 22).astimezone(timezone.utc))
    assert _local(deadline) == _br(2026, 11, 23, 15)


# 04. Opt-out inequívoco: nenhuma cobranca
def test_04_sem_interesse_parem():
    assert classify_signal("Não tenho interesse, parem") == "STOP_CONTACT"
    assert "NAO programe follow-up" in coaching_for_context(
        {"latest_customer_message": "Não tenho interesse, parem"}
    )


# 05. Somente fibra, sem condicionar a combo
def test_05_sem_movel():
    assert classify_signal("Quero fibra de 800 MB, móvel não preciso!") == "ONLY_FIBER"
    assert "nao condicione fechamento a movel" in coaching_for_context(
        {"latest_customer_message": "Móvel não preciso!"}
    )


# 06. Permanencia curta / fidelidade
def test_06_fidelidade_multa():
    assert classify_signal("Tenho apenas cinco meses no imóvel") == "FIDELITY"
    assert "excecao so com aprovacao" in coaching_for_context(
        {"latest_customer_message": "Tenho apenas cinco meses no imóvel"}
    )


# 07. Instalar amanha depende de cobertura/agenda
def test_07_instalacao_urgente_sem_promessa():
    assert classify_signal("Consegue instalar amanhã?") == "URGENCY_INSTALL"
    assert "consulte cobertura" in coaching_for_context(
        {"latest_customer_message": "Consegue instalar amanhã?"}
    )
    assert "Nao prometa ativacao amanha" in BASELINE_RULES


# 08. Golpe / inseguranca
def test_08_golpe_orientacao_oficial():
    assert classify_signal("Tenho medo de golpe, esse link é seguro?") == "SECURITY_CONCERN"
    assert "canais oficiais" in coaching_for_context(
        {"latest_customer_message": "Tenho medo de golpe"}
    )


# 09. "Assinei" e apenas fala do cliente, nao verificacao
def test_09_assinatura_declarada_nao_confirma_venda():
    assert classify_signal("Já assinei!") == "SIGNATURE_REPORTED"
    evidence = SalesEvidence(
        agreement=True, crm_opportunity_id=123, contract_sent_verified=True,
    )
    assert stage_from_evidence(evidence) == "CONTRATO_ENVIADO"
    assert stage_from_evidence(SalesEvidence(
        contract_sent_verified=True, signature_verified=True,
    )) == "ASSINATURA_CONFIRMADA"


# 10. Problema especifico no link
def test_10_link_nao_abre():
    assert classify_signal("O link não abre") == "SIGNATURE_ERROR"
    assert "consulte o estado real" in coaching_for_context(
        {"latest_customer_message": "O link não abre"}
    )


# 11. Falha CRM nao pode virar "registrado"
def test_11_crm_sem_id_nao_e_registro():
    assert stage_from_evidence(SalesEvidence(agreement=True, crm_opportunity_id=None)) == "ACORDO_COMERCIAL"
    assert stage_from_evidence(SalesEvidence(agreement=True, crm_opportunity_id=777)) == "CRM_REGISTRADO"


# 12. Filas protegidas / sem invasao
def test_12_ia_nao_muda_permissao_de_fila():
    assert "fila e da instancia autorizadas" in BASELINE_RULES
    assert "transferencia autorizada" in BASELINE_RULES


# 13. Historico de outro vendedor nao entra no treinamento
def test_13_historico_segregado():
    current = {"latest_customer_message": "Vou pensar"}
    context_with_other = {
        **current,
        "other_conversations": [{
            "phone": "55999990000",
            "customer": "NOME_SIGILOSO_DE_OUTRA_CONVERSA",
            "body": "SEGREDO_DE_OUTRO_VENDEDOR",
        }],
    }
    assert coaching_for_context(current) == coaching_for_context(context_with_other)
    assert "SEGREDO_DE_OUTRO_VENDEDOR" not in coaching_for_context(context_with_other)


# 14. Audio sem transcricao
def test_14_audio_nao_transcrito_nao_e_fato():
    assert classify_signal("[Áudio não transcrito]") == "AUDIO_UNTRANSCRIBED"
    assert "nao presuma o conteudo" in coaching_for_context(
        {"latest_customer_message": "[Áudio não transcrito]"}
    )


# 15. Credito reprovado, somente retorno oficial e contestacao permitida
def test_15_credito_reprovado_nao_e_aprovacao():
    assert classify_signal("Meu crédito foi reprovado") == "CREDIT_REJECTED"
    assert "Nao afirme aprovacao" in coaching_for_context(
        {"latest_customer_message": "Meu crédito foi reprovado"}
    )
    assert stage_from_evidence(SalesEvidence(
        crm_opportunity_id=88, credit_result_received=True,
    )) == "CONSULTA_CREDITO"


# 16. Cliente combinou retorno as 15h; nao antecipar
def test_16_retornar_as_15h():
    receive = _br(2026, 11, 23, 10).astimezone(timezone.utc)
    requested = requested_return_not_before("Pode me chamar às 15h", receive)
    assert _local(requested) == _br(2026, 11, 23, 15)


# Casos complementares: nao confundir dia de instalacao com horario de follow-up
@pytest.mark.parametrize("message", [
    "Consegue instalar amanhã?",
    "O preço até R$ 90 serve?",
    "Vou ficar cinco meses no imóvel",
    "Você consegue atender na segunda-feira?",
])
def test_nao_inventar_agenda_de_retorno(message):
    assert requested_return_not_before(
        message, _br(2026, 11, 22).astimezone(timezone.utc),
    ) is None


def test_mensagem_de_sabado_com_retorno_terca_as_14h():
    requested = requested_return_not_before(
        "Eu retorno terça às 14h",
        _br(2026, 11, 21).astimezone(timezone.utc),
    )
    assert _local(requested) == _br(2026, 11, 24, 14)


def test_sem_repetir_a_mesma_data_apos_restart():
    original = _br(2026, 11, 22, 11).astimezone(timezone.utc)
    first = requested_return_not_before("Respondo amanhã à tarde", original)
    second = requested_return_not_before("Respondo amanhã à tarde", original)
    assert first == second
    assert _local(first) == _br(2026, 11, 23, 15)


def test_14_estados_observaveis_e_ativacao_real():
    assert len(OBSERVABLE_STAGES) == 14
    assert len(set(OBSERVABLE_STAGES)) == 14
    assert stage_from_evidence(SalesEvidence(activation_verified=True)) == "CONTRATO_ATIVO"
    assert stage_from_evidence(SalesEvidence(interest=True)) == "INTERESSE_CONFIRMADO"
    assert stage_from_evidence(SalesEvidence(lost_confirmed=True)) == "PERDIDO"
    assert stage_from_evidence(SalesEvidence(human_transfer_confirmed=True)) == "TRANSFERIDO_A_HUMANO"
