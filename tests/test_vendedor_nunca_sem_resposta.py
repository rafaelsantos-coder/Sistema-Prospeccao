"""Vendedor IA nunca deixa o cliente em silêncio quando o CRM falha (04-05/11)."""

from __future__ import annotations

from types import SimpleNamespace

from app.models import AIClosingState
from app.services import ai_seller
from app.services.crm_sulnet import CRMIntegrationError
from app.services.zapi import ZApiError


def _crm(status=None, method="", path="", code="", message="erro"):
    return CRMIntegrationError(message, status_code=status, method=method, path=path, error_code=code)


def test_classificacao_das_falhas_do_crm():
    kind = ai_seller._closer_failure_kind
    assert kind(_crm(code="CRM_HTTP_ERROR")) == "transient"
    assert kind(_crm(502, "GET", "/api/opportunities/1")) == "transient"
    assert kind(_crm(530, "GET", "/api/opportunities/1")) == "transient"
    assert kind(_crm(500, "GET", "/api/opportunities/1")) == "transient"
    assert kind(_crm(500, "POST", "/api/capture/leads/1/credit")) == "transient"
    assert kind(_crm(500, "PATCH", "/api/opportunities/14373")) == "data"
    assert kind(_crm(422, "PATCH", "/api/opportunities/1")) == "data"
    assert kind(
        _crm(400, "POST", "/api/integracoes/agent/v1/opportunities/14370/contract/generate",
             message="Nenhum template de contrato para o produto: Meu Plano Essencial 2GB")
    ) == "contract"
    assert kind(_crm(401, "GET", "/api/opportunities/1")) == "config"
    assert kind(CRMIntegrationError("Checklist de Telefonia Móvel incompleto: numero_portado")) == "logic"
    assert kind(CRMIntegrationError("Vendedor IA tentou enviar sem propriedade exclusiva da conversa.")) == "ownership"
    assert kind(ZApiError("whatsapp disconnected")) == "transient"


def test_escalonamento_passa_para_consultor_e_avisa_o_cliente(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_seller, "_handoff_to_human", lambda db, **kw: calls.append(("handoff", kw["reason"])))
    monkeypatch.setattr(ai_seller, "_record_activity", lambda db, **kw: calls.append(("activity", kw["outcome"])))
    monkeypatch.setattr(
        ai_seller,
        "_send_text",
        lambda db, **kw: calls.append(("send", kw["text"], kw.get("handoff_notice"))),
    )
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", opportunity_id=14370, plan_label="2GB")
    ai_seller._escalate_closer_failure(
        None,
        state=state,
        conversation=SimpleNamespace(id=1),
        agent=SimpleNamespace(id=2),
        reason="Nenhum template de contrato para o produto: Meu Plano Essencial 2GB",
        notice=ai_seller.CONTRACT_HANDOFF_NOTICE,
    )
    assert calls[0][0] == "handoff" and "template" in calls[0][1]
    assert calls[1] == ("activity", "VENDEDOR_IA_TRANSFERIU")
    assert calls[2] == ("send", ai_seller.CONTRACT_HANDOFF_NOTICE, True)


def test_aviso_ao_cliente_nao_depende_do_guardiao():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="ESCALATED")
    for text in (
        ai_seller.CLOSER_HANDOFF_NOTICE,
        ai_seller.CONTRACT_HANDOFF_NOTICE,
        f"{ai_seller.DATA_RETRY_PREFIX} Qual é o seu CEP?",
    ):
        assert ai_seller._is_fixed_closing_text(state, text)
