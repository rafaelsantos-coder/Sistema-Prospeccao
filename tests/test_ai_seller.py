from types import SimpleNamespace

from app.services.crm_sulnet import (
    CRMIntegrationError,
    CRMPlaybookMissingItem,
    CRMPlaybookResult,
)

from app.services.ai_seller import (
    MOBILE_SIGNATURE_TYPE,
    _apply_mobile_answer,
    _apply_billing_form_answer,
    _billing_local,
    _extract_address_number,
    _extract_birth_date,
    _extract_cep,
    _extract_cpf,
    _extract_email,
    _extract_iccids,
    _extract_name,
    _contract_is_signed,
    _finalize_signed_sale,
    _finish_sale_contract,
    _prepare_contract_before_win,
    _win_idempotency_key,
    _mobile_missing_fields,
    _mobile_next_step,
    _mobile_values,
    _category_values,
    _playbook_code_key,
    _playbook_step_for_item,
    _prompt_for_step,
    _query_playbook_guidance,
    _redact,
    _sync_plan_context,
    _seller_followup_prompt,
    _seller_step_buttons,
    _yes_no,
)


def test_extract_name_requires_full_name():
    assert _extract_name("João Carlos da Silva") == "João Carlos da Silva"
    assert _extract_name("meu nome é Maria Oliveira") == "Maria Oliveira"
    assert _extract_name("João") is None


def test_extract_cpf_and_cep():
    assert _extract_cpf("Meu CPF é 123.456.789-01") == "12345678901"
    assert _extract_cpf("não informei") is None
    assert _extract_cep("CEP 98900-000") == "98900000"
    assert _extract_cep("98900000") == "98900000"


def test_extract_email():
    assert _extract_email("Pode usar Teste.Cliente+fibra@exemplo.com.br") == "teste.cliente+fibra@exemplo.com.br"
    assert _extract_email("não tenho agora") is None


def test_extract_address_number_and_complement():
    number, complement = _extract_address_number("número 123, apto 402")
    assert number == "123"
    assert "402" in complement

    number, complement = _extract_address_number("450 fundos")
    assert number == "450"
    assert complement.lower() == "fundos"


def test_billing_local():
    data = _billing_local("Quero boleto por e-mail, vencimento dia 10")
    assert data["paymentMethod"] == "Boleto"
    assert data["invoiceDelivery"] == "E-mail"
    assert data["dueDay"] == 10


def test_yes_no_is_conservative():
    assert _yes_no("sim, pode seguir") == "SIM"
    assert _yes_no("não, quero outro") == "NAO"
    assert _yes_no("talvez depois") == "NAO_INFORMADO"


def test_redact_removes_sensitive_customer_data():
    text = _redact("CPF 12345678901 CEP 98900-000 e-mail cliente@exemplo.com")
    assert "12345678901" not in text
    assert "98900-000" not in text
    assert "cliente@exemplo.com" not in text
    assert "[DOCUMENTO]" in text
    assert "[CEP]" in text
    assert "[EMAIL]" in text


def test_seller_dashboard_is_available_to_admin():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        login_page = client.get("/login")
        assert login_page.status_code == 200
        csrf = login_page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        response = client.post(
            "/login",
            data={
                "email": "admin@sulnet.local",
                "password": "Sulnet@2026!",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        page = client.get("/ai/seller")
        assert page.status_code == 200
        assert "Vendedor IA Sulnet" in page.text
        assert "Etapas do fechamento" in page.text



def test_extract_birth_date_normalizes_to_iso():
    assert _extract_birth_date("10/05/1990") == "1990-05-10"
    assert _extract_birth_date("1990-05-10") == "1990-05-10"
    assert _extract_birth_date("31/02/1990") is None



def test_mobile_contract_type_new_number_skips_iccid_number_due_day_and_fee():
    values = _mobile_values({})
    assert values["tipo_assinatura_contrato"] == MOBILE_SIGNATURE_TYPE

    ok, _ = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "numero_novo", values)
    assert ok is True
    assert values["tipo_contratacao"] == "Número novo"
    assert _mobile_next_step(values) == "MOBILE_PAYMENT_METHOD"


def test_mobile_contract_type_accepts_m2m_and_requests_quantity_in_fallback():
    values = _mobile_values({})
    ok, message = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "M2M", values)
    assert ok is True
    assert message == ""
    assert values["tipo_contratacao"] == "M2M"
    assert _mobile_next_step(values) == "MOBILE_M2M_QTY"


def test_mobile_portability_requires_number_and_holder_data_but_not_iccid_or_date():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número Portabilidade",
        "numero_portado": "5555999990000",
        "port_titular": "Sim",
    }
    missing = _mobile_missing_fields(values)
    assert "port_nome_titular" in missing
    assert "port_cpf_cnpj" in missing
    assert "port_iccid" not in missing
    assert "port_data" not in missing
    assert "vencimento" not in missing
    assert "taxa_ativacao" not in missing


def test_mobile_boleto_billing_requires_delivery_and_unification_reason():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número novo",
        "forma_pagamento": "Boleto",
        "unificar_faturas": "Não",
    }
    missing = _mobile_missing_fields(values)
    assert "envio_fatura" in missing
    assert "unificar_motivo" in missing
    assert "vencimento" not in missing
    assert "taxa_ativacao" not in missing

    values["envio_fatura"] = ["WhatsApp", "E-mail"]
    values["unificar_motivo"] = "Faturas separadas"
    assert _mobile_missing_fields(values) == []


def test_mobile_debit_requires_bank_details():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número novo",
        "forma_pagamento": "Débito em Conta",
        "unificar_faturas": "Sim",
    }
    missing = _mobile_missing_fields(values)
    assert "debito_banco" in missing
    assert "debito_agencia" in missing
    assert "debito_conta" in missing
    assert "debito_titular" in missing


def test_mobile_next_step_for_portability_goes_directly_to_number():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número Portabilidade",
    }
    assert _mobile_next_step(values) == "MOBILE_PORT_NUMBER"




class _MobilePlanContextClient:
    def __init__(self):
        self.updated = []

    def get_opportunity(self, opportunity_id):
        assert opportunity_id == 11754
        return {"opportunityId": opportunity_id, "planId": None}

    def products(self, query=""):
        if query:
            return []
        return [
            {
                "id": 392,
                "name": "Meu Trabalho Comigo 15GB",
                "monthlyValue": 69.80,
                "categoryId": 83,
                "categoryName": "TELEFONIA MÓVEL",
            },
            {
                "id": 393,
                "name": "Meu Trabalho Comigo 20GB",
                "monthlyValue": 79.80,
                "categoryId": 83,
                "categoryName": "TELEFONIA MÓVEL",
            },
        ]

    def update_opportunity(self, opportunity_id, payload):
        self.updated.append((opportunity_id, payload))
        return {"opportunityId": opportunity_id}

    def update_category_form(
        self,
        opportunity_id,
        values,
        *,
        category_id=None,
        plan_id=None,
    ):
        self.category_updates.append(
            (opportunity_id, dict(values), category_id, plan_id)
        )
        return {"opportunityId": opportunity_id}


def test_sync_plan_context_recovers_mobile_product_after_email():
    client = _MobilePlanContextClient()
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        offer_price=69.80,
    )
    collected = {}

    _sync_plan_context(client, state, collected)

    assert collected["planId"] == 392
    assert collected["categoryId"] == 83
    assert collected["categoryName"] == "TELEFONIA MÓVEL"
    assert collected["catalogPlanName"] == "Meu Trabalho Comigo 15GB"
    assert state.plan_label == "Sulmóvel 15GB"
    assert client.updated == [
        (
            11754,
            {
                "planId": 392,
                "planLabel": "Meu Trabalho Comigo 15GB",
            },
        )
    ]


def test_seller_step_buttons_cover_predefined_mobile_questions():
    assert _seller_step_buttons("MOBILE_CONTRACT_TYPE") == [
        {"id": "portabilidade", "label": "Portabilidade"},
        {"id": "numero_novo", "label": "Número novo"},
        {"id": "m2m", "label": "M2M"},
    ]
    assert _seller_step_buttons("MOBILE_PORT_TITULAR") == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]
    assert _seller_step_buttons("MOBILE_PAYMENT_METHOD") == [
        {"id": "boleto", "label": "Boleto"},
        {"id": "debito", "label": "Débito em conta"},
    ]
    assert _seller_step_buttons("MOBILE_INVOICE_DELIVERY") == [
        {"id": "whatsapp", "label": "WhatsApp"},
        {"id": "email", "label": "E-mail"},
        {"id": "ambos", "label": "Ambos"},
    ]
    assert _seller_step_buttons("MOBILE_UNIFY") == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]


def test_seller_step_buttons_keep_free_text_steps_without_buttons():
    assert _seller_step_buttons("MOBILE_PORT_NUMBER", "Qual é o número que será portado?") == []
    assert _seller_step_buttons("EMAIL", "Qual é o seu e-mail?") == []


def test_seller_followup_keeps_exact_closing_step():
    state = SimpleNamespace(step="MOBILE_UNIFY", plan_label="Sulmóvel 15GB")
    text = _seller_followup_prompt(state, {}, followup_step=1)
    assert "unificar" in text.lower()
    assert _seller_step_buttons(state.step, text) == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]

    state.step = "MOBILE_CONTRACT_TYPE"
    text = _seller_followup_prompt(state, {}, followup_step=2)
    assert "sulmóvel" in text.lower()
    assert _seller_step_buttons(state.step, text) == [
        {"id": "portabilidade", "label": "Portabilidade"},
        {"id": "numero_novo", "label": "Número novo"},
        {"id": "m2m", "label": "M2M"},
    ]


def test_seller_followup_does_not_invent_new_question_for_free_text_step():
    state = SimpleNamespace(step="CEP", plan_label="800 Mega")
    text = _seller_followup_prompt(state, {}, followup_step=1)
    assert "cep" in text.lower()
    assert _seller_step_buttons(state.step, text) == []



class _PlaybookClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.playbook_calls = 0
        self.updated = []
        self.category_updates = []

    def playbook(self, opportunity_id, *, category):
        self.playbook_calls += 1
        if not self.responses:
            raise AssertionError("Playbook chamado além do esperado.")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def update_opportunity(self, opportunity_id, payload):
        self.updated.append((opportunity_id, payload))
        return {"opportunityId": opportunity_id}

    def update_category_form(
        self,
        opportunity_id,
        values,
        *,
        category_id=None,
        plan_id=None,
    ):
        self.category_updates.append(
            (opportunity_id, dict(values), category_id, plan_id)
        )
        return {"opportunityId": opportunity_id}


def _playbook_result(*, missing=None, next_question="", ready=False, category="fibra"):
    return CRMPlaybookResult(
        opportunity_id=11754,
        protocol="CRM-TEST",
        category=category,
        crm_ok=True,
        next_step="playbook",
        next_question=next_question,
        ready_for_win=ready,
        credit_status="Aprovado",
        credit_approved=True,
        credit_rejected=False,
        sdr_ia_funnel=True,
        missing=list(missing or []),
    )


def test_playbook_uses_crm_next_question_for_supported_core_field():
    item = CRMPlaybookMissingItem(
        code="clientEmail",
        label="E-mail",
        question="Qual seu melhor e-mail?",
        patch_key="clientEmail",
    )
    client = _PlaybookClient([
        _playbook_result(
            missing=[item],
            next_question="Qual seu melhor e-mail?",
        )
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="PLAN_CONFIRM",
    )
    collected = {}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="EMAIL",
    )

    assert step == "EMAIL"
    assert question == "Qual seu melhor e-mail?"
    assert collected["_playbook_mode"] == "active"
    assert collected["_playbook_step"] == "EMAIL"
    assert collected["_playbook_code"] == "clientEmail"
    assert collected["_playbook_missing_codes"] == ["clientEmail"]


def test_playbook_syncs_known_value_before_asking_next_question():
    missing_email = CRMPlaybookMissingItem(
        code="clientEmail",
        question="Qual seu e-mail?",
        patch_key="clientEmail",
    )
    missing_cep = CRMPlaybookMissingItem(
        code="cep",
        question="Qual é o CEP da instalação?",
        patch_key="cep",
    )
    client = _PlaybookClient([
        _playbook_result(missing=[missing_email], next_question="Qual seu e-mail?"),
        _playbook_result(missing=[missing_cep], next_question="Qual é o CEP da instalação?"),
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="EMAIL",
    )
    collected = {"email": "cliente@exemplo.test"}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="CEP",
    )

    assert client.updated == [
        (11754, {"clientEmail": "cliente@exemplo.test"})
    ]
    assert client.playbook_calls == 2
    assert step == "CEP"
    assert question == "Qual é o CEP da instalação?"


def test_playbook_404_disables_it_for_same_closing_and_uses_fallback():
    client = _PlaybookClient([
        CRMIntegrationError("Rota não encontrada.", status_code=404)
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="CPF",
    )
    collected = {}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="CEP",
    )
    assert step == "CEP"
    assert question is None
    assert collected["_playbook_mode"] == "fallback"
    assert collected["_playbook_fallback_reason"] == "HTTP_404"

    step2, question2 = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="ADDRESS_NUMBER",
    )
    assert step2 == "ADDRESS_NUMBER"
    assert question2 is None
    assert client.playbook_calls == 1


def test_playbook_mobile_existing_field_maps_to_existing_closer_step():
    item = CRMPlaybookMissingItem(
        code="tipo_contratacao",
        question="Seu chip será linha nova, portabilidade ou M2M?",
        patch_key="productInfo.categoryForm.values.tipo_contratacao",
    )
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="EMAIL",
    )
    collected = {"categoryName": "Telefonia Móvel"}

    assert _playbook_step_for_item(item, state, collected) == "MOBILE_CONTRACT_TYPE"


def test_phase3_mobile_iccid_is_now_driven_by_playbook():
    item = CRMPlaybookMissingItem(
        code="novo_iccid",
        question="Qual é o ICCID do chip?",
        patch_key="productInfo.categoryForm.values.novo_iccid",
    )
    client = _PlaybookClient([
        _playbook_result(
            missing=[item],
            next_question="Qual é o ICCID do chip?",
            category="telefonia-movel",
        )
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="MOBILE_CONTRACT_TYPE",
    )
    collected = {"categoryName": "Telefonia Móvel"}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="MOBILE_PAYMENT_METHOD",
    )

    assert step == "MOBILE_NEW_ICCID"
    assert question == "Qual é o ICCID do chip?"
    assert collected["_playbook_code"] == "novo_iccid"
    assert collected["_playbook_mode"] == "active"


def test_prompt_for_step_prefers_playbook_question_when_step_matches():
    state = SimpleNamespace(
        step="EMAIL",
        plan_label="800 Mega",
    )
    collected = {
        "_playbook_step": "EMAIL",
        "_playbook_question": "Qual e-mail deseja usar no contrato?",
    }

    assert _prompt_for_step(state, collected) == "Qual e-mail deseja usar no contrato?"


def test_playbook_code_normalization_handles_snake_and_camel_case():
    assert _playbook_code_key("clientEmail") == "clientemail"
    assert _playbook_code_key("tipo_contratacao") == "tipocontratacao"
    assert _playbook_code_key("port_cpf_cnpj") == "portcpfcnpj"



def test_phase3_mobile_new_line_fields_are_validated():
    values = _mobile_values({})

    ok, _ = _apply_mobile_answer("MOBILE_NEW_ICCID", "8955000000000000001", values)
    assert ok is True
    assert values["novo_iccid"] == "8955000000000000001"

    ok, _ = _apply_mobile_answer("MOBILE_NEW_NUMBER", "(55) 99999-0000", values)
    assert ok is True
    assert values["numero_novo"] == "55999990000"


def test_phase3_portability_supports_iccid_and_date():
    values = _mobile_values({})

    ok, _ = _apply_mobile_answer("MOBILE_PORT_ICCID", "8955000000000000002", values)
    assert ok is True
    assert values["port_iccid"] == "8955000000000000002"

    ok, _ = _apply_mobile_answer("MOBILE_PORT_DATE", "15/11/2026", values)
    assert ok is True
    assert values["port_data"] == "2026-11-15"


def test_phase3_m2m_requires_exact_iccid_count():
    values = _mobile_values({})
    ok, _ = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "M2M", values)
    assert ok is True

    ok, _ = _apply_mobile_answer("MOBILE_M2M_QTY", "2", values)
    assert ok is True
    assert values["m2m_quantidade"] == 2

    ok, message = _apply_mobile_answer(
        "MOBILE_M2M_ICCIDS",
        "8955000000000000001",
        values,
    )
    assert ok is False
    assert "2 ICCID" in message

    ok, _ = _apply_mobile_answer(
        "MOBILE_M2M_ICCIDS",
        "8955000000000000001, 8955000000000000002",
        values,
    )
    assert ok is True
    assert values["m2m_iccids"] == [
        "8955000000000000001",
        "8955000000000000002",
    ]


def test_extract_iccids_keeps_multiple_distinct_values():
    assert _extract_iccids(
        "8955000000000000001, 8955000000000000002"
    ) == [
        "8955000000000000001",
        "8955000000000000002",
    ]


def test_phase3_mobile_due_day_and_activation_fee():
    values = _mobile_values({})

    ok, _ = _apply_mobile_answer("MOBILE_DUE_DAY", "dia 10", values)
    assert ok is True
    assert values["vencimento"] == 10

    ok, _ = _apply_mobile_answer("MOBILE_ACTIVATION_FEE", "Sim", values)
    assert ok is True
    assert values["taxa_ativacao"] == "Sim"

    ok, _ = _apply_mobile_answer("MOBILE_ACTIVATION_VALUE", "R$ 49,90", values)
    assert ok is True
    assert values["taxa_valor"] == 49.90


def test_phase3_fiber_billing_uses_category_form_fields():
    values = _category_values({})

    ok, _ = _apply_billing_form_answer("BILLING_PAYMENT_METHOD", "Boleto", values)
    assert ok is True
    assert values["forma_pagamento"] == "Boleto"

    ok, _ = _apply_billing_form_answer("BILLING_INVOICE_DELIVERY", "WhatsApp e e-mail", values)
    assert ok is True
    assert values["envio_fatura"] == ["WhatsApp", "E-mail"]

    ok, _ = _apply_billing_form_answer("BILLING_DUE_DAY", "vencimento dia 15", values)
    assert ok is True
    assert values["vencimento"] == 15

    assert values["tipo_assinatura_contrato"] == MOBILE_SIGNATURE_TYPE


def test_phase3_playbook_maps_billing_differently_for_fiber_and_mobile():
    item = CRMPlaybookMissingItem(
        code="vencimento",
        question="Qual o vencimento?",
        patch_key="productInfo.categoryForm.values.vencimento",
    )

    fiber_state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="EMAIL",
    )
    assert _playbook_step_for_item(item, fiber_state, {}) == "BILLING_DUE_DAY"

    mobile_state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="EMAIL",
    )
    assert _playbook_step_for_item(
        item,
        mobile_state,
        {"categoryName": "Telefonia Móvel"},
    ) == "MOBILE_DUE_DAY"


def test_phase3_signature_is_automatic_and_never_becomes_customer_question():
    item = CRMPlaybookMissingItem(
        code="tipo_assinatura_contrato",
        question="Como deseja assinar?",
        patch_key="productInfo.categoryForm.values.tipo_assinatura_contrato",
    )
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="EMAIL",
    )
    values = _category_values({})
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert _playbook_step_for_item(item, state, {}) is None



def test_phase3_signature_is_auto_persisted_then_playbook_advances():
    signature = CRMPlaybookMissingItem(
        code="tipo_assinatura_contrato",
        question="Como deseja assinar?",
        patch_key="productInfo.categoryForm.values.tipo_assinatura_contrato",
    )
    due_day = CRMPlaybookMissingItem(
        code="vencimento",
        question="Qual dia de vencimento?",
        patch_key="productInfo.categoryForm.values.vencimento",
    )
    client = _PlaybookClient([
        _playbook_result(
            missing=[signature],
            next_question="Como deseja assinar?",
            category="fibra",
        ),
        _playbook_result(
            missing=[due_day],
            next_question="Qual dia de vencimento?",
            category="fibra",
        ),
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="EMAIL",
    )
    collected = {
        "planId": 800,
        "categoryId": 10,
        "categoryName": "FIBRA",
    }

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="BILLING",
    )

    assert step == "BILLING_DUE_DAY"
    assert question == "Qual dia de vencimento?"
    assert client.category_updates
    _, values, category_id, plan_id = client.category_updates[0]
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert category_id == 10
    assert plan_id == 800


def test_phase3_structured_buttons_include_m2m_and_billing_choices():
    assert _seller_step_buttons("MOBILE_CONTRACT_TYPE")[-1] == {
        "id": "m2m",
        "label": "M2M",
    }
    assert _seller_step_buttons("MOBILE_ACTIVATION_FEE") == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]
    assert _seller_step_buttons("BILLING_PAYMENT_METHOD") == [
        {"id": "boleto", "label": "Boleto"},
        {"id": "debito", "label": "Débito em conta"},
    ]
    assert _seller_step_buttons("BILLING_UNIFY") == [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ]



class _Phase4ContractClient:
    def __init__(
        self,
        *,
        agent_generate=True,
        contract=True,
        generated=None,
        started=None,
        won=None,
    ):
        self.agent_generate = agent_generate
        self.contract = contract
        self.generated = dict(generated or {})
        self.started = dict(started or {})
        self.won = dict(
            won
            or {
                "alreadyWon": False,
                "opportunity": {"status": "won"},
            }
        )
        self.generate_calls = 0
        self.start_calls = 0
        self.mark_won_calls = []

    def supports_any(self, *capabilities):
        return self.agent_generate

    def supports(self, capability):
        return capability == "contract" and self.contract

    def generate_agent_contract(self, opportunity_id):
        self.generate_calls += 1
        return dict(self.generated)

    def start_contract(self, opportunity_id):
        self.start_calls += 1
        return dict(self.started)

    def mark_won(self, opportunity_id, *, idempotency_key=None):
        self.mark_won_calls.append((opportunity_id, idempotency_key))
        return dict(self.won)


def _phase4_state(**overrides):
    data = {
        "opportunity_id": 11754,
        "external_id": "prospecta-conversation-99",
        "step": "BILLING_DUE_DAY",
        "contract_status": "",
        "sign_url": "",
        "completed_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_phase4_ready_for_win_generates_contract_before_any_win():
    client = _Phase4ContractClient(
        generated={
            "status": "aguardando_assinatura",
            "signUrl": "https://sign.example/11754",
        },
    )
    state = _phase4_state()
    collected = {"_playbook_ready_for_win": True}

    reply = _finish_sale_contract(
        client,
        state,
        collected,
    )

    assert client.generate_calls == 1
    assert client.mark_won_calls == []
    assert state.step == "WAITING_SIGNATURE"
    assert state.sign_url == "https://sign.example/11754"
    assert "assinar" in reply.lower()


def test_phase4_not_ready_never_generates_contract_or_marks_won():
    client = _Phase4ContractClient()
    state = _phase4_state()
    collected = {"_playbook_ready_for_win": False}

    reply = _prepare_contract_before_win(
        client,
        state,
        collected,
    )

    assert client.generate_calls == 0
    assert client.start_calls == 0
    assert client.mark_won_calls == []
    assert state.step == "WAITING_CRM_READY"
    assert state.contract_status == "aguardando_validacao_crm"
    assert "crm" in reply.lower()


def test_phase4_contract_without_sign_url_stays_pending_and_not_won():
    client = _Phase4ContractClient(
        generated={
            "status": "gerado",
            "pdfUrl": "https://crm.example/contrato.pdf",
        },
        contract=False,
    )
    state = _phase4_state()
    collected = {"_playbook_ready_for_win": True}

    _finish_sale_contract(
        client,
        state,
        collected,
    )

    assert state.step == "WAITING_CONTRACT"
    assert state.contract_status == "gerado"
    assert client.mark_won_calls == []


def test_phase4_signed_contract_marks_won_with_stable_idempotency_key():
    client = _Phase4ContractClient()
    state = _phase4_state(
        step="WAITING_SIGNATURE",
        contract_status="assinado",
    )
    collected = {}

    expected_key = _win_idempotency_key(state)
    result = _finalize_signed_sale(
        client,
        state,
        collected,
    )

    assert result["opportunity"]["status"] == "won"
    assert client.mark_won_calls == [(11754, expected_key)]
    assert collected["_win_idempotency_key"] == expected_key
    assert collected["_won_after_signature"] is True
    assert state.step == "COMPLETED"
    assert state.completed_at is not None


def test_phase4_idempotency_key_is_deterministic_across_retries():
    state1 = _phase4_state()
    state2 = _phase4_state()

    assert _win_idempotency_key(state1) == _win_idempotency_key(state2)


def test_phase4_unsigned_contract_cannot_mark_won():
    client = _Phase4ContractClient()
    state = _phase4_state(
        step="WAITING_SIGNATURE",
        contract_status="aguardando_assinatura",
    )
    collected = {}

    try:
        _finalize_signed_sale(
            client,
            state,
            collected,
        )
    except CRMIntegrationError as exc:
        assert exc.error_code == "CRM_CONTRACT_NOT_SIGNED"
    else:
        raise AssertionError("Contrato não assinado não pode ser marcado como ganho.")

    assert client.mark_won_calls == []


def test_phase4_contract_status_normalization_is_conservative():
    assert _contract_is_signed("assinado") is True
    assert _contract_is_signed("SIGNED") is True
    assert _contract_is_signed("concluído") is True
    assert _contract_is_signed("aguardando_assinatura") is False
    assert _contract_is_signed("contrato_gerado") is False


def test_phase4_playbook_without_missing_but_not_ready_waits_crm():
    client = _PlaybookClient([
        _playbook_result(
            missing=[],
            ready=False,
            category="fibra",
        )
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="BILLING_DUE_DAY",
    )
    collected = {}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="__FINISH__",
    )

    assert step == "WAITING_CRM_READY"
    assert question is None
    assert collected["_playbook_ready_for_win"] is False


def test_phase4_ready_playbook_releases_contract_generation():
    client = _PlaybookClient([
        _playbook_result(
            missing=[],
            ready=True,
            category="fibra",
        )
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        credit_status="",
        step="BILLING_DUE_DAY",
    )
    collected = {}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="__FINISH__",
    )

    assert step == "__FINISH__"
    assert question is None
    assert collected["_playbook_ready_for_win"] is True
