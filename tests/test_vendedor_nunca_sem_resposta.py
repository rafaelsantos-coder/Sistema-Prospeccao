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


def test_falha_do_crm_mantem_a_venda_com_a_ia_e_avisa_uma_vez(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_seller, "_record_activity", lambda db, **kw: calls.append(("activity", kw["outcome"])))
    monkeypatch.setattr(ai_seller, "write_audit", lambda db, **kw: calls.append(("audit", kw["action"])))
    monkeypatch.setattr(ai_seller, "_send_text", lambda db, **kw: calls.append(("send", kw["text"])))
    state = AIClosingState(
        conversation_id=1, agent_id=2, external_id="x", opportunity_id=14370, plan_label="2GB", step="WAITING_CRM_READY",
    )
    for _ in range(2):
        ai_seller._keep_sale_with_ai(
            None,
            state=state,
            conversation=SimpleNamespace(id=1),
            agent=SimpleNamespace(id=2, user=None),
            reason="Nenhum template de contrato para o produto: Meu Plano Essencial 2GB",
            notice=ai_seller.CONTRACT_HOLD_NOTICE,
        )
    assert state.step == "WAITING_CRM_READY"
    assert ("activity", "VENDEDOR_IA_BLOQUEIO") in calls
    assert ("audit", "AI_CLOSER_BLOCKED") in calls
    assert [item for item in calls if item[0] == "send"] == [("send", ai_seller.CONTRACT_HOLD_NOTICE)]
    assert "consultor" not in ai_seller.CONTRACT_HOLD_NOTICE.lower()


def test_job_bloqueado_volta_a_tentar_mais_tarde():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="WAITING_CRM_READY")
    job = SimpleNamespace(status="PROCESSANDO", attempts=3, locked_at=object(), available_at=None)
    ai_seller._retry_job_later(job, state)
    assert job.status == "PENDENTE" and job.attempts == 0 and job.locked_at is None
    for _ in range(ai_seller.AI_HOLD_MAX_RETRIES):
        ai_seller._retry_job_later(job, state)
    assert job.status == "FALHOU"


def test_avisos_ao_cliente_nao_dependem_do_guardiao():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="WAITING_CRM_READY")
    for text in (
        ai_seller.CLOSER_HOLD_NOTICE,
        ai_seller.CONTRACT_HOLD_NOTICE,
        f"{ai_seller.AI_OWNS_NOTICE} Qual é o seu CPF?",
        f"{ai_seller.DATA_RETRY_PREFIX} Qual é o seu CEP?",
    ):
        assert ai_seller._is_fixed_closing_text(state, text)


def test_nenhum_aviso_do_vendedor_chama_consultor():
    for text in (
        ai_seller.CLOSER_HOLD_NOTICE,
        ai_seller.CONTRACT_HOLD_NOTICE,
        ai_seller.SALE_DECLINE_NOTICE,
        ai_seller.COMBO_MOBILE_DECLINE_NOTICE,
        ai_seller.AI_OWNS_NOTICE,
    ):
        assert "consultor" not in text.lower() and "atendente" not in text.lower()


def test_recusa_do_resumo_a_ia_ajusta(monkeypatch):
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step=ai_seller.SALE_ADJUST_STEP, plan_label="800 Mega")
    collected = {}
    reply = ai_seller._handle_sale_adjustment(None, state=state, conversation=SimpleNamespace(id=1), collected=collected, text="a forma de pagamento")
    assert state.step == "BILLING_PAYMENT_METHOD" and reply

    state.step = ai_seller.SALE_ADJUST_STEP
    reply = ai_seller._handle_sale_adjustment(None, state=state, conversation=SimpleNamespace(id=1), collected=collected, text="quero outro plano")
    assert state.step == ai_seller.PLAN_CHANGE_STEP and "plano" in reply

    state.step = ai_seller.SALE_ADJUST_STEP
    reply = ai_seller._handle_sale_adjustment(None, state=state, conversation=SimpleNamespace(id=1), collected=collected, text="desisti")
    assert state.step == "COMPLETED"

    state.step = ai_seller.SALE_ADJUST_STEP
    reply = ai_seller._handle_sale_adjustment(None, state=state, conversation=SimpleNamespace(id=1), collected=collected, text="hmm")
    assert state.step == ai_seller.SALE_ADJUST_STEP and "ajustar" in reply


def test_venda_escalada_antiga_volta_para_a_ia():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="ESCALATED", opportunity_id=14370)
    assert ai_seller._resume_step_for(state, {}) == "WAITING_CRM_READY"
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="ESCALATED", sign_url="https://z/s", contract_status="PENDENTE")
    assert ai_seller._resume_step_for(state, {}) == "WAITING_SIGNATURE"
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="ESCALATED")
    assert ai_seller._resume_step_for(state, {"name": "Ana Souza"}) == "CPF"


def test_resumo_ja_mostra_o_produto_certo_do_crm():
    from decimal import Decimal

    class _Client:
        def get_opportunity(self, opportunity_id):
            return {"planId": 800}

        def products(self, query=""):
            if query:
                return []
            return [
                {"id": 800, "name": "SULNET RESID FIBRA - 800MB - 2-25", "monthlyValue": 119.90, "categoryId": 10, "categoryName": "FIBRA"},
                {"id": 392, "name": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26", "monthlyValue": 69.80, "categoryId": 83, "categoryName": "TELEFONIA MÓVEL"},
            ]

        def update_opportunity(self, opportunity_id, payload):
            return {}

    state = AIClosingState(
        conversation_id=1, agent_id=2, external_id="x", step="WAITING_CRM_READY",
        opportunity_id=14373, plan_label="Sulmóvel 15GB", offer_price=Decimal("69.80"),
    )
    collected = {"catalogPlanName": "SULNET RESID FIBRA - 800MB - 2-25"}
    reply = ai_seller._finish_sale_contract(_Client(), state, collected)
    assert state.step == "CONFIRM_SALE"
    assert "15GB" in reply and "Fibra 800" not in reply


def test_recusa_do_movel_no_combo_nao_diz_que_nada_foi_contratado():
    assert "internet continua contratada" in ai_seller.COMBO_MOBILE_DECLINE_NOTICE
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step=ai_seller.SALE_ADJUST_STEP)
    assert ai_seller._is_fixed_closing_text(state, ai_seller.COMBO_MOBILE_DECLINE_NOTICE)
    assert ai_seller._is_fixed_closing_text(state, ai_seller.RESUME_NOTICE)


def test_mensagem_anterior_ao_resumo_nao_responde_o_resumo():
    from app.database import SessionLocal
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Contact, Conversation, Message

    with TestClient(app):
        db = SessionLocal()
        try:
            contact = Contact(phone="5555900000546", name="Cliente Aceite")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, status="EM_ATENDIMENTO", team="DIGITAL")
            db.add(conversation)
            db.flush()
            antiga = Message(conversation_id=conversation.id, contact_id=contact.id, direction="INBOUND", message_type="TEXT", body="Trocar para 15GB")
            db.add(antiga)
            db.flush()
            resumo = Message(
                conversation_id=conversation.id, contact_id=contact.id, direction="OUTBOUND", message_type="TEXT",
                body="Antes de gerar o contrato, confira o resumo da sua contratação: ...",
            )
            db.add(resumo)
            db.flush()
            nova = Message(conversation_id=conversation.id, contact_id=contact.id, direction="INBOUND", message_type="TEXT", body="Confirmo")
            db.add(nova)
            db.flush()
            assert ai_seller._message_predates_summary(db, conversation.id, antiga)
            assert not ai_seller._message_predates_summary(db, conversation.id, nova)
        finally:
            db.rollback()
            db.close()


class _PortClient:
    def __init__(self):
        self.patches = []

    def update_playbook_field(self, opportunity_id, *, patch_key, value):
        self.patches.append((patch_key, value))
        return {}


def test_portabilidade_sem_data_usa_a_data_da_venda():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", opportunity_id=14373, plan_label="Sulmóvel 15GB")
    collected = {"categoryFormValues": {"tipo_contratacao": "Número Portabilidade", "numero_portado": "55999002508"}}
    client = _PortClient()
    assert ai_seller._ensure_port_date(client, state, collected)
    key, value = client.patches[0]
    assert key == "productInfo.categoryForm.values.port_data"
    assert len(value) == 10 and value[4] == "-" and value[7] == "-"
    assert not ai_seller._ensure_port_date(client, state, collected)


def test_numero_novo_nao_precisa_de_data_de_portabilidade():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", opportunity_id=1)
    client = _PortClient()
    assert not ai_seller._ensure_port_date(client, state, {"categoryFormValues": {"tipo_contratacao": "Número novo"}})
    assert client.patches == []


def test_pendencia_de_data_de_portabilidade_preenche_e_gera_de_novo():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", opportunity_id=14373)
    collected = {"categoryFormValues": {"tipo_contratacao": "Número Portabilidade", "port_data": "2026-11-05"}}
    client = _PortClient()
    exc = CRMIntegrationError(
        "Campos pendentes para contrato: Data portabilidade",
        status_code=400,
        method="POST",
        path="/api/integracoes/agent/v1/opportunities/14373/contract/generate",
    )
    assert ai_seller._recover_contract_pending_field(client, state, collected, exc) is None
    assert client.patches and client.patches[0][0] == "productInfo.categoryForm.values.port_data"
    assert collected["_contract_recovery_reason"] == "mobile_contract_type_synced"
    client.patches.clear()
    assert ai_seller._recover_contract_pending_field(client, state, collected, exc) is None
    assert client.patches == []


def test_funil_sdr_ia_nao_espera_o_cliente_nas_etapas_de_faturamento():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="BILLING_PAYMENT_METHOD")
    collected = {"_sdr_ia_funnel": True}
    reply = ai_seller._sdr_ia_auto_advance(
        state, collected, "Vou seguir apenas com os dados necessários para concluir sua contratação pelo canal digital."
    )
    assert state.step == "WAITING_CRM_READY"
    assert not reply.startswith("Vou seguir apenas")


def test_fora_do_funil_sdr_ia_nada_muda():
    state = AIClosingState(conversation_id=1, agent_id=2, external_id="x", step="BILLING_PAYMENT_METHOD")
    assert ai_seller._sdr_ia_auto_advance(state, {}, "Qual a forma de pagamento?") == "Qual a forma de pagamento?"
    assert state.step == "BILLING_PAYMENT_METHOD"


# ---- Consulta ao CRM em análise de crédito/assinatura não fica em loop (07/11) ----

def _status_state(step="WAITING_SIGNATURE"):
    return AIClosingState(
        id=4, conversation_id=1, agent_id=2, external_id="x", opportunity_id=14370,
        plan_label="2GB", step=step, collected_json="{}",
    )


def _quiet_seller(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_seller, "_record_activity", lambda db, **kw: calls.append(("activity", kw["outcome"])))
    monkeypatch.setattr(ai_seller, "write_audit", lambda db, **kw: calls.append(("audit", kw["action"])))
    monkeypatch.setattr(ai_seller, "_send_text", lambda db, **kw: calls.append(("send", kw["text"])))
    monkeypatch.setattr(ai_seller, "_send_step_message", lambda db, **kw: calls.append(("send", kw["text"])))
    return calls


def _hold(state, exc):
    return ai_seller._hold_status_poll_failure(
        None,
        state=state,
        conversation=SimpleNamespace(id=1),
        agent=SimpleNamespace(id=2, user=None),
        exc=exc,
        step=state.step,
    )


def test_lead_sumido_do_crm_segura_a_consulta_e_avisa_a_supervisao_uma_vez(monkeypatch):
    from app.timeutils import utcnow

    calls = _quiet_seller(monkeypatch)
    state = _status_state()
    not_found = _crm(404, "GET", "/api/opportunities/14370", message="Oportunidade não encontrada.")
    assert _hold(state, not_found) is False
    assert _hold(state, not_found) is False
    assert _hold(state, not_found) is True
    assert state.updated_at > utcnow() + ai_seller.AI_HOLD_RETRY / 2
    for _ in range(5):
        assert _hold(state, not_found) is True
    assert calls.count(("activity", "VENDEDOR_IA_BLOQUEIO")) == 1
    assert [item for item in calls if item[0] == "send"] == []
    assert state.step == "WAITING_SIGNATURE"


def test_instabilidade_do_crm_so_segura_depois_de_20_falhas(monkeypatch):
    _quiet_seller(monkeypatch)
    state = _status_state("CREDIT_REVIEW")
    outage = _crm(502, "GET", "/api/opportunities/14370")
    assert not any(_hold(state, outage) for _ in range(19))
    assert _hold(state, outage) is True


def test_conversa_de_humano_nao_entra_em_espera(monkeypatch):
    _quiet_seller(monkeypatch)
    state = _status_state()
    owned = CRMIntegrationError("Vendedor IA tentou enviar sem propriedade exclusiva da conversa.")
    assert not any(_hold(state, owned) for _ in range(25))


def test_consulta_bem_sucedida_zera_a_contagem(monkeypatch):
    _quiet_seller(monkeypatch)
    state = _status_state()
    not_found = _crm(404, "GET", "/api/opportunities/14370")
    for _ in range(3):
        _hold(state, not_found)
    collected = ai_seller._load_collected(state)
    ai_seller._clear_status_poll_failures(state, collected)
    assert ai_seller.STATUS_POLL_FAILURES_KEY not in ai_seller._load_collected(state)
    assert ai_seller.STATUS_POLL_ALERT_KEY not in ai_seller._load_collected(state)
    assert _hold(state, not_found) is False


def test_poller_em_analise_de_credito_para_de_consultar_a_cada_45s(monkeypatch):
    from app.models import AIAgentProfile, Conversation
    from app.timeutils import utcnow

    calls = _quiet_seller(monkeypatch)
    state = _status_state("CREDIT_REVIEW")

    class FakeCRM:
        def get_status(self, opportunity_id):
            raise _crm(404, "GET", f"/api/opportunities/{opportunity_id}", message="Oportunidade não encontrada.")

    agent = SimpleNamespace(id=2, user=None, active=True, execution_mode="AUTO")
    conversation = SimpleNamespace(id=1)
    fake_db = SimpleNamespace(
        scalars=lambda stmt: [state],
        get=lambda model, pk: {Conversation: conversation, AIAgentProfile: agent}.get(model),
    )
    monkeypatch.setattr(ai_seller, "seller_operational", lambda: True)
    monkeypatch.setattr(ai_seller.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM()))

    for _ in range(3):
        ai_seller.poll_pending_contracts(fake_db)
    assert state.updated_at > utcnow() + ai_seller.AI_HOLD_RETRY / 2
    assert state.step == "CREDIT_REVIEW"
    assert calls.count(("activity", "VENDEDOR_IA_BLOQUEIO")) == 1
    assert [item for item in calls if item[0] == "send"] == []
