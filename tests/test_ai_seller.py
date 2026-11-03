from dataclasses import replace
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
    _complete_if_crm_already_won,
    _finalize_signed_sale,
    _finish_sale_contract,
    _prepare_contract_before_win,
    _persist_category_form,
    _win_idempotency_key,
    _mobile_missing_fields,
    _mobile_next_step,
    _mobile_values,
    _category_values,
    _canonical_playbook_value,
    _playbook_code_key,
    _playbook_recovery_metadata,
    _playbook_step_for_item,
    _prompt_for_step,
    _query_playbook_guidance,
    _redact,
    _sync_plan_context,
    _seller_followup_prompt,
    _seller_contract_knowledge_reply,
    _seller_existing_customer_plan_change_requested,
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



def test_sdr_ia_mobile_new_number_uses_defaults_and_skips_forbidden_fields():
    collected = {
        "_sdr_ia_funnel": True,
        "_sdr_ia_defaults": {
            "vencimento": "10",
            "taxa_ativacao": "Não",
            "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        },
    }
    values = _mobile_values(collected)
    assert values["vencimento"] == "10"
    assert values["forma_pagamento"] == "Boleto"
    assert values["envio_fatura"] == ["WhatsApp"]
    assert values["taxa_ativacao"] == "Não"
    assert values["tipo_assinatura_contrato"] == MOBILE_SIGNATURE_TYPE

    ok, _ = _apply_mobile_answer(
        "MOBILE_CONTRACT_TYPE",
        "numero_novo",
        values,
        sdr_ia_funnel=True,
    )
    assert ok is True
    assert values["tipo_contratacao"] == "Número novo"
    # Boleto + WhatsApp + vencimento já são defaults homologados do SDR IA,
    # então não resta outra pergunta de faturamento nesta rota.
    assert _mobile_next_step(values, sdr_ia_funnel=True) is None


def test_non_sdr_mobile_contract_type_still_accepts_m2m():
    values = _mobile_values({})
    ok, message = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "M2M", values)
    assert ok is True
    assert message == ""
    assert values["tipo_contratacao"] == "M2M"
    assert _mobile_next_step(values) == "MOBILE_M2M_QTY"


def test_sdr_ia_mobile_contract_type_rejects_m2m():
    values = _mobile_values({"_sdr_ia_funnel": True})
    ok, message = _apply_mobile_answer(
        "MOBILE_CONTRACT_TYPE",
        "M2M",
        values,
        sdr_ia_funnel=True,
    )
    assert ok is False
    assert "Portabilidade" in message
    assert "Número novo" in message
    assert "M2M" not in message


def test_mobile_portability_does_not_repeat_holder_data_when_same_holder():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número Portabilidade",
        "numero_portado": "5555999990000",
        "port_titular": "Sim",
        "forma_pagamento": "Boleto",
        "envio_fatura": ["WhatsApp"],
        "unificar_faturas": "Sim",
    }
    missing = _mobile_missing_fields(values)
    assert "port_nome_titular" not in missing
    assert "port_cpf_cnpj" not in missing
    assert "port_iccid" not in missing
    assert "port_data" not in missing


def test_mobile_portability_asks_holder_data_only_when_different_holder():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número Portabilidade",
        "numero_portado": "5555999990000",
        "port_titular": "Não",
    }
    missing = _mobile_missing_fields(values)
    assert "port_nome_titular" in missing
    assert "port_cpf_cnpj" in missing


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
                "name": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26",
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

    def update_playbook_field(
        self,
        opportunity_id,
        *,
        patch_key,
        value,
    ):
        self.playbook_patches.append(
            (opportunity_id, patch_key, value)
        )
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
    assert collected["catalogPlanName"] == "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26"
    assert state.plan_label == "Sulmóvel 15GB"
    assert client.updated == [
        (
            11754,
            {
                "planId": 392,
                "planLabel": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26",
                "catalogMonthlyValue": 69.80,
            },
        )
    ]
    assert collected["contractProductName"] == "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26"


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
        self.playbook_patches = []

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

    def update_playbook_field(
        self,
        opportunity_id,
        *,
        patch_key,
        value,
    ):
        self.playbook_patches.append(
            (opportunity_id, patch_key, value)
        )
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


def _playbook_result(
    *,
    missing=None,
    next_question="",
    ready=False,
    category="fibra",
    sdr_ia_funnel=False,
    sdr_defaults=None,
    field_policy=None,
):
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
        sdr_ia_funnel=sdr_ia_funnel,
        sdr_ia_defaults=dict(sdr_defaults or {}),
        sdr_ia_field_policy=dict(field_policy or {}),
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

    assert client.playbook_patches == [
        (11754, "clientEmail", "cliente@exemplo.test")
    ]
    assert client.playbook_calls == 2
    assert step == "CEP"
    assert question == "Qual é o CEP da instalação?"


def test_mobile_known_contract_type_uses_exact_crm_patch_key():
    item = CRMPlaybookMissingItem(
        code="cf_tipo_contratacao",
        label="Tipo de contratação móvel",
        question="Como você quer contratar o Sulmóvel?",
        patch_key="productInfo.categoryForm.values.tipo_contratacao",
    )
    client = _PlaybookClient([
        _playbook_result(
            missing=[item],
            next_question=item.question,
            category="telefonia-movel",
        ),
        _playbook_result(
            missing=[],
            ready=True,
            category="telefonia-movel",
        ),
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="WAITING_CONTRACT",
    )
    collected = {
        "categoryName": "Telefonia Móvel",
        "categoryFormValues": {
            "tipo_contratacao": "Número novo",
        },
    }

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="__FINISH__",
    )

    assert client.playbook_patches == [
        (
            11754,
            "productInfo.categoryForm.values.tipo_contratacao",
            "Número novo",
        )
    ]
    assert step == "__FINISH__"
    assert question is None


def test_unknown_playbook_field_never_falls_through_to_finish():
    item = CRMPlaybookMissingItem(
        code="cf_nova_regra",
        label="Nova regra",
        question="Qual opção deseja?",
        patch_key="customFields.cf_nova_regra",
        options=[
            {"id": "a", "label": "Opção A", "value": "A"},
            {"id": "b", "label": "Opção B", "value": "B"},
        ],
    )
    client = _PlaybookClient([
        _playbook_result(
            missing=[item],
            next_question=item.question,
            category="telefonia-movel",
        )
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="WAITING_CONTRACT",
    )
    collected = {"categoryName": "Telefonia Móvel"}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="__FINISH__",
    )

    assert step == "PLAYBOOK_DYNAMIC_FIELD"
    assert question == "Qual opção deseja?"
    assert collected["_playbook_dynamic_patch_key"] == "customFields.cf_nova_regra"
    assert _seller_step_buttons(step, question, collected) == [
        {"id": "a", "label": "Opção A"},
        {"id": "b", "label": "Opção B"},
    ]


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


def test_playbook_canonical_value_uses_crm_option_value():
    item = CRMPlaybookMissingItem(
        code="cf_tipo_contratacao",
        patch_key="productInfo.categoryForm.values.tipo_contratacao",
        options=[
            {
                "id": "portabilidade",
                "label": "Portabilidade",
                "value": "PORTABILITY",
            },
            {
                "id": "numero_novo",
                "label": "Número novo",
                "value": "NEW_NUMBER",
            },
        ],
    )

    matched, value = _canonical_playbook_value(
        item,
        "Número Portabilidade",
    )

    assert matched is True
    assert value == "PORTABILITY"


def test_playbook_canonical_value_refuses_ambiguous_option():
    item = CRMPlaybookMissingItem(
        code="cf_tipo_contratacao",
        options=[
            {"id": "novo_a", "label": "Novo A", "value": "A"},
            {"id": "novo_b", "label": "Novo B", "value": "B"},
        ],
    )

    matched, value = _canonical_playbook_value(item, "novo")

    assert matched is False
    assert value is None


def test_playbook_recovery_metadata_requires_current_crm_field():
    state = SimpleNamespace(step="MOBILE_CONTRACT_TYPE")
    collected = {
        "_playbook_step": "MOBILE_CONTRACT_TYPE",
        "_playbook_code": "cf_tipo_contratacao",
        "_playbook_patch_key": (
            "productInfo.categoryForm.values.tipo_contratacao"
        ),
    }

    metadata = _playbook_recovery_metadata(state, collected)

    assert metadata["crm_playbook_required"] is True
    assert metadata["crm_playbook_step"] == "MOBILE_CONTRACT_TYPE"
    assert metadata["crm_playbook_code"] == "cf_tipo_contratacao"


def test_mapped_playbook_step_uses_crm_options_as_buttons():
    collected = {
        "_playbook_step": "MOBILE_CONTRACT_TYPE",
        "_playbook_options": [
            {
                "id": "portabilidade",
                "label": "Portabilidade",
                "value": "PORTABILITY",
            },
            {
                "id": "numero_novo",
                "label": "Número novo",
                "value": "NEW_NUMBER",
            },
        ],
    }

    assert _seller_step_buttons(
        "MOBILE_CONTRACT_TYPE",
        "Como você quer contratar o Sulmóvel?",
        collected,
    ) == [
        {"id": "portabilidade", "label": "Portabilidade"},
        {"id": "numero_novo", "label": "Número novo"},
    ]


def test_playbook_code_normalization_handles_snake_and_camel_case():
    assert _playbook_code_key("clientEmail") == "clientemail"
    assert _playbook_code_key("tipo_contratacao") == "tipocontratacao"
    assert _playbook_code_key("port_cpf_cnpj") == "portcpfcnpj"
    assert _playbook_code_key("cf_envio_fatura") == "enviofatura"
    assert _playbook_code_key("cf_port_data") == "portdata"


def test_cf_invoice_delivery_maps_to_billing_step():
    item = CRMPlaybookMissingItem(
        code="cf_envio_fatura",
        question="Como deseja receber a fatura?",
    )
    state = SimpleNamespace(plan_label="800 Mega")
    collected = {}

    assert (
        _playbook_step_for_item(item, state, collected)
        == "BILLING_INVOICE_DELIVERY"
    )


def test_cf_port_data_is_skipped_in_sdr_ia_funnel():
    item = CRMPlaybookMissingItem(
        code="cf_port_data",
        question="Qual a data da portabilidade?",
    )
    state = SimpleNamespace(plan_label="Sulmóvel 15GB")
    collected = {"_sdr_ia_funnel": True}

    assert _playbook_step_for_item(item, state, collected) is None



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


def test_non_sdr_fiber_billing_keeps_full_form_behavior():
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

    assert "tipo_assinatura_contrato" not in values


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


def test_sdr_ia_signature_is_automatic_and_never_becomes_customer_question():
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
    collected = {"_sdr_ia_funnel": True}
    values = _category_values(collected)
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert values["vencimento"] == "10"
    assert values["taxa_ativacao"] == "Não"
    assert _playbook_step_for_item(item, state, collected) is None



def test_sdr_ia_defaults_are_persisted_before_next_billing_question():
    payment = CRMPlaybookMissingItem(
        code="forma_pagamento",
        question="Como deseja pagar: boleto ou débito?",
        patch_key="productInfo.categoryForm.values.forma_pagamento",
    )
    iccid = CRMPlaybookMissingItem(
        code="novo_iccid",
        question="Qual é o ICCID?",
        patch_key="productInfo.categoryForm.values.novo_iccid",
    )
    defaults = {
        "vencimento": "10",
        "taxa_ativacao": "Não",
        "tipo_assinatura_contrato": "Assinatura Digital IA",
    }
    client = _PlaybookClient([
        _playbook_result(
            missing=[payment],
            next_question=payment.question,
            category="fibra",
            sdr_ia_funnel=True,
            sdr_defaults=defaults,
        ),
        _playbook_result(
            missing=[iccid, payment],
            next_question=iccid.question,
            category="fibra",
            sdr_ia_funnel=True,
            sdr_defaults=defaults,
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
        fallback_step="WAITING_CRM_READY",
    )

    assert step == "WAITING_CRM_READY"
    assert question is None
    assert client.playbook_calls == 2
    assert client.category_updates
    _, values, category_id, plan_id = client.category_updates[0]
    assert values["vencimento"] == "10"
    assert values["forma_pagamento"] == "Boleto"
    assert values["envio_fatura"] == ["WhatsApp"]
    assert values["taxa_ativacao"] == "Não"
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert "novo_iccid" not in values
    assert category_id == 10
    assert plan_id == 800


def test_phase3_structured_buttons_include_m2m_and_billing_choices():
    assert _seller_step_buttons("MOBILE_CONTRACT_TYPE")[-1] == {
        "id": "m2m",
        "label": "M2M",
    }
    assert _seller_step_buttons(
        "MOBILE_CONTRACT_TYPE",
        collected={"_sdr_ia_funnel": True},
    ) == [
        {"id": "portabilidade", "label": "Portabilidade"},
        {"id": "numero_novo", "label": "Número novo"},
    ]
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




def test_sdr_ia_boleto_does_not_require_unification_due_day_or_fee():
    values = {
        "tipo_contratacao": "Número novo",
        "forma_pagamento": "Boleto",
        "envio_fatura": ["WhatsApp"],
        "vencimento": "10",
        "taxa_ativacao": "Não",
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
    }
    assert _mobile_missing_fields(values, sdr_ia_funnel=True) == []
    assert _mobile_next_step(values, sdr_ia_funnel=True) is None


def test_sdr_ia_playbook_skips_due_fee_unify_iccid_and_m2m_fields():
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="EMAIL",
    )
    collected = {
        "_sdr_ia_funnel": True,
        "categoryName": "Telefonia Móvel",
    }
    skipped_codes = [
        "novo_iccid",
        "port_iccid",
        "port_data",
        "m2m_quantidade",
        "m2m_iccids",
        "vencimento",
        "forma_pagamento",
        "envio_fatura",
        "taxa_ativacao",
        "taxa_valor",
        "unificar_faturas",
        "unificar_motivo",
        "tipo_assinatura_contrato",
    ]
    for code in skipped_codes:
        item = CRMPlaybookMissingItem(
            code=code,
            question=f"Pergunta proibida {code}",
            patch_key=f"productInfo.categoryForm.values.{code}",
        )
        assert _playbook_step_for_item(item, state, collected) is None


def test_non_sdr_playbook_keeps_full_mobile_fields_available():
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        credit_status="",
        step="EMAIL",
    )
    collected = {"categoryName": "Telefonia Móvel"}
    assert _playbook_step_for_item(
        CRMPlaybookMissingItem(code="novo_iccid"),
        state,
        collected,
    ) == "MOBILE_NEW_ICCID"
    assert _playbook_step_for_item(
        CRMPlaybookMissingItem(code="m2m_quantidade"),
        state,
        collected,
    ) == "MOBILE_M2M_QTY"
    assert _playbook_step_for_item(
        CRMPlaybookMissingItem(code="vencimento"),
        state,
        collected,
    ) == "MOBILE_DUE_DAY"



def test_sdr_ia_defaults_override_stale_values_in_every_billing_patch():
    client = _PlaybookClient([])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="800 Mega",
        offer_price=119.90,
    )
    collected = {
        "_sdr_ia_funnel": True,
        "_sdr_ia_defaults": {
            "vencimento": "10",
            "taxa_ativacao": "Não",
            "tipo_assinatura_contrato": "Assinatura Digital IA",
        },
        "planId": 800,
        "categoryId": 10,
        "categoryName": "FIBRA",
    }
    stale = {
        "forma_pagamento": "Boleto",
        "envio_fatura": ["WhatsApp"],
        "vencimento": "15",
        "taxa_ativacao": "Sim",
        "tipo_assinatura_contrato": "Manual",
    }

    _persist_category_form(
        client,
        state,
        collected,
        stale,
    )

    _, sent, category_id, plan_id = client.category_updates[-1]
    assert sent["forma_pagamento"] == "Boleto"
    assert sent["vencimento"] == "10"
    assert sent["taxa_ativacao"] == "Não"
    assert sent["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert category_id == 10
    assert plan_id == 800


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

    def generate_agent_contract(
        self,
        opportunity_id,
        *,
        send_signature=True,
        require_ready_for_win=True,
    ):
        self.generate_calls += 1
        self.last_generate_args = {
            "opportunity_id": opportunity_id,
            "send_signature": send_signature,
            "require_ready_for_win": require_ready_for_win,
        }
        return dict(self.generated)

    def start_contract(self, opportunity_id):
        self.start_calls += 1
        return dict(self.started)

    def mark_won(self, opportunity_id, *, idempotency_key=None):
        self.mark_won_calls.append((opportunity_id, idempotency_key))
        return dict(self.won)

    def get_status(self, opportunity_id):
        return {
            "opportunity": {
                "id": opportunity_id,
                "status": "open",
            },
            "contract": {
                "status": "aguardando_assinatura",
            },
        }

    def get_opportunity(self, opportunity_id):
        return {"opportunityId": opportunity_id, "planId": 800}

    def products(self, query=""):
        return [
            {
                "id": 800,
                "name": "SULNET RESID FIBRA - 800MB - 2-25 (sem desconto)",
                "monthlyValue": 119.90,
                "categoryId": 10,
                "categoryName": "FIBRA",
            }
        ]

    def update_opportunity(self, opportunity_id, payload):
        return {"opportunityId": opportunity_id, **payload}


def _phase4_state(**overrides):
    data = {
        "opportunity_id": 11754,
        "external_id": "prospecta-conversation-99",
        "plan_label": "800 Mega",
        "offer_price": 119.90,
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
    collected = {"_playbook_ready_for_win": True, "_sale_accepted": True}

    reply = _finish_sale_contract(
        client,
        state,
        collected,
    )

    assert client.generate_calls == 1
    assert client.last_generate_args == {
        "opportunity_id": 11754,
        "send_signature": True,
        "require_ready_for_win": True,
    }
    assert client.mark_won_calls == []
    assert state.step == "WAITING_SIGNATURE"
    assert state.sign_url == "https://sign.example/11754"
    assert "assinar" in reply.lower()
    assert "48 horas" in reply
    assert collected["_contract_product_name"] == (
        "SULNET RESID FIBRA - 800MB - 2-25 (sem desconto)"
    )


def test_phase4_sdr_skipped_fields_allow_crm_to_decide_readiness():
    client = _Phase4ContractClient(
        generated={
            "status": "aguardando_assinatura",
            "signUrl": "https://sign.example/11771",
            "signatureReused": True,
        },
    )
    state = _phase4_state(opportunity_id=11771)
    collected = {
        "_playbook_ready_for_win": False,
        "_contract_generation_allowed": True,
    }

    reply = _prepare_contract_before_win(
        client,
        state,
        collected,
    )

    assert client.generate_calls == 1
    assert state.step == "WAITING_SIGNATURE"
    assert state.sign_url == "https://sign.example/11771"
    assert "assinar" in reply.lower()


def test_mobile_contract_400_recovers_to_contract_type_with_crm_options():
    item = CRMPlaybookMissingItem(
        code="cf_tipo_contratacao",
        label="Tipo de contratação móvel",
        question="Como você quer contratar sua linha móvel?",
        patch_key="productInfo.categoryForm.values.tipo_contratacao",
        options=[
            {
                "id": "portabilidade",
                "label": "Portabilidade",
                "value": "PORTABILITY",
            },
            {
                "id": "numero_novo",
                "label": "Número novo",
                "value": "NEW_NUMBER",
            },
        ],
    )

    class _RecoveryClient(_Phase4ContractClient):
        def generate_agent_contract(
            self,
            opportunity_id,
            *,
            send_signature=True,
            require_ready_for_win=True,
        ):
            self.generate_calls += 1
            raise CRMIntegrationError(
                "Campos pendentes para contrato: Tipo de contratação móvel",
                status_code=400,
                method="POST",
                path=(
                    f"/api/integracoes/agent/v1/opportunities/"
                    f"{opportunity_id}/contract/generate"
                ),
                response_body={
                    "error": (
                        "Campos pendentes para contrato: "
                        "Tipo de contratação móvel"
                    )
                },
            )

        def playbook(self, opportunity_id, *, category):
            assert category == "telefonia-movel"
            return _playbook_result(
                missing=[item],
                next_question=item.question,
                category="telefonia-movel",
                sdr_ia_funnel=True,
            )

        def get_opportunity(self, opportunity_id):
            return {"opportunityId": opportunity_id, "planId": 1500}

        def products(self, query=""):
            return [
                {
                    "id": 1500,
                    "name": "Meu Trabalho Comigo 15GB",
                    "monthlyValue": 49.90,
                    "categoryId": 20,
                    "categoryName": "Telefonia Móvel",
                }
            ]

    client = _RecoveryClient(contract=False)
    state = _phase4_state(
        opportunity_id=11754,
        plan_label="Sulmóvel 15GB",
        offer_price=49.90,
    )
    collected = {
        "_playbook_ready_for_win": False,
        "_contract_generation_allowed": True,
        "_sdr_ia_funnel": True,
        "planId": 1500,
        "categoryId": 20,
        "categoryName": "Telefonia Móvel",
        "categoryFormValues": {
            "tipo_contratacao": "Número Portabilidade",
            "numero_portado": "55999990000",
        },
    }

    reply = _prepare_contract_before_win(
        client,
        state,
        collected,
    )

    assert client.generate_calls == 1
    assert state.step == "MOBILE_CONTRACT_TYPE"
    assert state.contract_status == "aguardando_tipo_contratacao"
    assert "contratar" in reply.lower()
    assert "tipo_contratacao" not in collected["categoryFormValues"]
    assert collected["_playbook_code"] == "cf_tipo_contratacao"
    assert collected["_playbook_patch_key"] == (
        "productInfo.categoryForm.values.tipo_contratacao"
    )
    assert _seller_step_buttons(
        state.step,
        reply,
        collected,
    ) == [
        {"id": "portabilidade", "label": "Portabilidade"},
        {"id": "numero_novo", "label": "Número novo"},
    ]


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
    collected = {"_playbook_ready_for_win": True, "_sale_accepted": True}

    _finish_sale_contract(
        client,
        state,
        collected,
    )

    assert state.step == "WAITING_CONTRACT"
    assert state.contract_status == "gerado"
    assert client.mark_won_calls == []


def test_phase4_crm_already_won_completes_without_second_mark_won():
    client = _Phase4ContractClient()
    client.get_status = lambda opportunity_id: {
        "opportunity": {
            "id": opportunity_id,
            "status": "won",
            "backofficeCopy": {"id": 99001},
        },
        "contract": {"status": "signed"},
    }
    state = _phase4_state(
        step="WAITING_SIGNATURE",
        contract_status="aguardando_assinatura",
    )
    collected = {}

    result = _complete_if_crm_already_won(
        client,
        state,
        collected,
    )

    assert result is not None
    assert client.mark_won_calls == []
    assert state.step == "COMPLETED"
    assert collected["_won_confirmed_by_crm"] is True
    assert collected["_backoffice_opportunity_id"] == 99001


def test_phase4_agent_contract_waits_for_crm_auto_win_after_signature():
    client = _Phase4ContractClient()
    state = _phase4_state(
        step="WAITING_SIGNATURE",
        contract_status="assinado",
    )
    collected = {"_contract_generation_requested": True}

    result = _finalize_signed_sale(
        client,
        state,
        collected,
    )

    assert result["pendingAutoWin"] is True
    assert client.mark_won_calls == []
    assert state.step == "WAITING_SIGNATURE"
    assert collected["_awaiting_crm_auto_win"] is True


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
    assert _contract_is_signed("assinatura_concluida") is True
    assert _contract_is_signed("concluído") is False
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


def test_seller_detects_existing_customer_plan_upgrade_from_recent_messages():
    class FakeDB:
        def scalars(self, statement):
            return ["Queria aumentar", "Já tenho um plano"]

    assert _seller_existing_customer_plan_change_requested(
        FakeDB(),
        123,
        "Queria aumentar",
    ) is True


def test_seller_does_not_redirect_new_sale_without_existing_plan_context():
    class FakeDB:
        def scalars(self, statement):
            return ["Quero contratar", "Queria mais velocidade"]

    assert _seller_existing_customer_plan_change_requested(
        FakeDB(),
        123,
        "Queria mais velocidade",
    ) is False


def test_seller_contract_knowledge_answers_ftth_fidelity_without_sending_customer_to_contract():
    state = SimpleNamespace(plan_label="RES_800")
    reply = _seller_contract_knowledge_reply(
        state,
        {},
        "qual a fidelidade?",
    )

    assert reply is not None
    assert "12 meses" in reply
    assert "800 Mega" in reply
    assert "consultar" not in reply.lower()
    assert "contrato" not in reply.lower() or "novo termo" in reply.lower()


def test_seller_contract_knowledge_answers_contract_term_as_12_month_permanence():
    state = SimpleNamespace(plan_label="Plano 800 Mega")
    reply = _seller_contract_knowledge_reply(
        state,
        {},
        "qual o prazo do contrato?",
    )

    assert reply is not None
    assert "12 meses" in reply
    assert "fidelidade" in reply.lower() or "permanência" in reply.lower()


def test_seller_contract_knowledge_answers_no_fidelity_price_from_official_catalog():
    state = SimpleNamespace(plan_label="800 Mega")
    reply = _seller_contract_knowledge_reply(
        state,
        {},
        "tem opção sem fidelidade?",
    )

    assert reply is not None
    assert "R$ 159,90" in reply
    assert "R$ 860,00" in reply
    assert "R$ 119,90" in reply


def test_seller_contract_knowledge_keeps_mobile_out_of_ftth_rules():
    state = SimpleNamespace(plan_label="Sulmóvel 15GB")
    reply = _seller_contract_knowledge_reply(
        state,
        {"categoryName": "Telefonia Móvel"},
        "qual a fidelidade?",
    )

    assert reply is None


def test_seller_contract_penalty_after_eight_months_returns_real_value():
    state = SimpleNamespace(
        plan_label="800 Mega",
        offer_price=119.90,
        handoff_summary="",
    )
    reply = _seller_contract_knowledge_reply(
        state,
        {"freeMonths": 0},
        "qual o valor da multa de contrato, se eu só usar 8 meses?",
    )

    assert reply is not None
    assert "faltam 4 meses" in reply
    assert "R$ 860,00" in reply
    assert "R$ 286,67" in reply


def test_seller_contract_penalty_includes_two_free_months():
    state = SimpleNamespace(
        plan_label="800 Mega",
        offer_price=119.90,
        handoff_summary="",
    )
    reply = _seller_contract_knowledge_reply(
        state,
        {"freeMonths": 2},
        "se eu cancelar depois de 8 meses qual a multa?",
    )

    assert reply is not None
    assert "2 mensalidades grátis" in reply
    assert "R$ 1.099,80" in reply
    assert "R$ 366,60" in reply


def test_seller_contract_penalty_after_twelve_months_is_zero():
    state = SimpleNamespace(
        plan_label="800 Mega",
        offer_price=119.90,
        handoff_summary="",
    )
    reply = _seller_contract_knowledge_reply(
        state,
        {"freeMonths": 0},
        "se eu cancelar depois de 12 meses tem multa?",
    )

    assert reply is not None
    assert "não há multa de permanência" in reply.lower()


def _penalty_state():
    return SimpleNamespace(plan_label="800 Mega", offer_price=119.90, handoff_summary="")


def test_seller_contract_penalty_understands_ordinal_month():
    # Caso real da conversa 114: "qual a multa se eu quiser cancelar no setimo mes ?"
    reply = _seller_contract_knowledge_reply(
        _penalty_state(),
        {"freeMonths": 0},
        "qual a multa se eu quiser cancelar no setimo mes ?",
    )

    assert reply is not None
    assert "faltam 5 meses" in reply
    assert "R$ 358,33" in reply


def test_seller_contract_penalty_understands_numeric_ordinal():
    reply = _seller_contract_knowledge_reply(
        _penalty_state(),
        {"freeMonths": 0},
        "e a multa se eu cancelar no 7º mês?",
    )

    assert reply is not None
    assert "faltam 5 meses" in reply


def test_seller_contract_penalty_uses_bare_number_after_asking_months():
    collected = {"freeMonths": 0}
    first = _seller_contract_knowledge_reply(
        _penalty_state(),
        collected,
        "qual o valor da multa de contrato?",
    )
    assert first is not None
    assert "quantos meses" in first
    assert collected.get("_pending_penalty_months") is True

    second = _seller_contract_knowledge_reply(_penalty_state(), collected, "7")

    assert second is not None
    assert "faltam 5 meses" in second
    assert "R$ 358,33" in second
    assert "_pending_penalty_months" not in collected


def test_seller_contract_penalty_pending_question_does_not_capture_other_answers():
    collected = {"freeMonths": 0, "_pending_penalty_months": True}

    reply = _seller_contract_knowledge_reply(_penalty_state(), collected, "Boleto")

    assert reply is None
    assert "_pending_penalty_months" not in collected


def test_billing_merge_keeps_previous_answers_across_messages():
    # Sequência real da conversa 62: "10" -> "Boleto" -> "10" entrava em ciclo.
    from app.services.ai_seller import _billing_merge

    collected = {}
    first = _billing_merge(collected, "Boleto")
    assert first == {"paymentMethod": "Boleto"}

    collected["paymentMethod"] = first["paymentMethod"]
    second = _billing_merge(collected, "10")
    assert second == {"paymentMethod": "Boleto", "dueDay": 10}


def test_billing_merge_keeps_due_day_when_payment_comes_later():
    from app.services.ai_seller import _billing_merge

    billing = _billing_merge({"dueDay": 10}, "Boleto")

    assert billing == {"paymentMethod": "Boleto", "dueDay": 10}


def test_billing_merge_ignores_bare_number_before_payment_method():
    from app.services.ai_seller import _billing_merge

    billing = _billing_merge({}, "10")

    assert "dueDay" not in billing


def test_billing_prompt_and_buttons_ask_one_thing_at_a_time():
    from app.services.ai_seller import _prompt_for_step, _seller_step_buttons

    state = SimpleNamespace(step="BILLING", plan_label="800 Mega")

    assert "forma de pagamento" in _prompt_for_step(state, {})
    assert "vencimento" not in _prompt_for_step(state, {})
    assert {b["label"] for b in _seller_step_buttons("BILLING", "", {})} == {"Boleto", "Débito em conta"}

    paid = {"paymentMethod": "Boleto"}
    assert "vencimento" in _prompt_for_step(state, paid)
    assert _seller_step_buttons("BILLING", "", paid) == []


def test_playbook_category_id_is_persisted_with_resolved_plan():
    # Conversa 240: o CRM pedia categoryId e o Vendedor perguntava ao cliente em
    # ciclo. Com plano e categoria já resolvidos no catálogo, grava sem perguntar.
    item = CRMPlaybookMissingItem(
        code="categoryId",
        label="Categoria do produto",
        question="Confirme a categoria do produto (Telefonia Móvel).",
        patch_key="",
    )
    client = _PlaybookClient([
        _playbook_result(missing=[item], next_question=item.question),
        _playbook_result(missing=[], ready=True),
    ])
    state = SimpleNamespace(
        opportunity_id=11754,
        plan_label="Fibra 800 Mega",
        offer_price=119.90,
        credit_status="Aprovado",
        step="WAITING_CONTRACT",
    )
    collected = {"planId": 77, "categoryId": 3, "categoryName": "Internet"}

    step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step="__FINISH__",
    )

    assert step == "__FINISH__"
    assert question is None
    assert [(opp, cat, plan) for opp, _values, cat, plan in client.category_updates] == [
        (11754, 3, 77),
    ]


def test_closer_ownership_allows_only_the_final_handoff_notice():
    from app.services.ai_seller import CRMIntegrationError, _assert_closer_owns_conversation

    agent = SimpleNamespace(id=2, role_type="CLOSER")
    conversation = SimpleNamespace(id=240)

    class _Db:
        def __init__(self, state):
            self.state = state

        def scalar(self, _query):
            return SimpleNamespace(state=self.state, agent_id=2)

    _assert_closer_owns_conversation(_Db("CLOSING"), conversation=conversation, agent=agent)
    _assert_closer_owns_conversation(
        _Db("HANDOFF_HUMAN"),
        conversation=conversation,
        agent=agent,
        handoff_notice=True,
    )
    try:
        _assert_closer_owns_conversation(_Db("HANDOFF_HUMAN"), conversation=conversation, agent=agent)
    except CRMIntegrationError:
        pass
    else:
        raise AssertionError("Mensagem comum não pode sair depois do handoff humano.")


def test_seller_followup_blocked_by_guardian_is_cancelled_not_retried(monkeypatch):
    # Conversa 240: o bloqueio escapava, o worker desfazia a transação e o mesmo
    # follow-up voltava a cada 10 minutos.
    from app.services import ai_seller
    from app.services.ai_guardian import GuardianBlockedError, GuardianResult

    conversation = SimpleNamespace(id=240)
    state = SimpleNamespace(step="CPF", conversation_id=240)
    job = SimpleNamespace(id=359, conversation_id=240, step=3, status="PROCESSANDO", error=None, source_message_id=None)
    agent = SimpleNamespace(user=None)
    audits = []

    class _Db:
        def get(self, _model, _id):
            return conversation

        def scalar(self, _query):
            return state

    def _blocked(*_args, **_kwargs):
        raise GuardianBlockedError(
            GuardianResult(verdict="BLOCK", issue_codes=["REPEATED_QUESTION", "CATEGORY_CONFLICT"])
        )

    scheduled = []
    monkeypatch.setattr(ai_seller, "_load_collected", lambda _state: {})
    monkeypatch.setattr(ai_seller, "_seller_followup_prompt", lambda *_a, **_k: "Pode me enviar o CPF?")
    monkeypatch.setattr(ai_seller, "_send_step_message", _blocked)
    monkeypatch.setattr(ai_seller, "_schedule_seller_followup", lambda *_a, **_k: scheduled.append(1))
    monkeypatch.setattr(ai_seller, "write_audit", lambda *_a, **kw: audits.append(kw))

    outcome = ai_seller.process_seller_followup(_Db(), job, agent)

    assert outcome == "guardian_blocked"
    assert job.status == "CANCELADO"
    assert "REPEATED_QUESTION" in job.error
    assert scheduled == []
    assert audits and audits[0]["action"] == "AI_CLOSER_FOLLOWUP_BLOCKED"


def test_contract_waits_for_explicit_acceptance():
    """Conversa 114: o contrato saiu sem o cliente ter aceitado o resumo."""
    client = _Phase4ContractClient(
        generated={"status": "aguardando_assinatura", "signUrl": "https://sign.example/1"},
    )
    state = _phase4_state()
    collected = {
        "_playbook_ready_for_win": True,
        "paymentMethod": "Boleto",
        "dueDay": 10,
    }

    reply = _finish_sale_contract(client, state, collected)

    assert client.generate_calls == 0
    assert state.step == "CONFIRM_SALE"
    assert "800 Mega" in reply
    assert "119,90" in reply
    assert "Boleto" in reply
    assert "dia 10" in reply
    assert "confirma" in reply.lower()

    collected["_sale_accepted"] = True
    _finish_sale_contract(client, state, collected)
    assert client.generate_calls == 1
    assert state.step == "WAITING_SIGNATURE"


def test_sale_acceptance_answer_reads_buttons_and_free_text():
    from app.services.ai_seller import _sale_acceptance_answer

    assert _sale_acceptance_answer("Confirmo") == "SIM"
    assert _sale_acceptance_answer("sim, pode gerar") == "SIM"
    assert _sale_acceptance_answer("Quero ajustar") == "NAO"
    assert _sale_acceptance_answer("não, quero pensar melhor") == "NAO"
    assert _sale_acceptance_answer("qual a fidelidade?") == "NAO_INFORMADO"


def test_confirm_sale_step_has_buttons_and_reminder_prompt():
    from app.services.ai_seller import _prompt_for_step, _seller_step_buttons

    state = _phase4_state(step="CONFIRM_SALE")
    labels = [item["label"] for item in _seller_step_buttons("CONFIRM_SALE", "", {})]
    assert labels == ["Confirmo", "Quero ajustar"]
    prompt = _prompt_for_step(state, {})
    assert "confirma" in prompt.lower()
    assert "800 Mega" in prompt


def test_seller_question_detection_keeps_step_answers():
    from app.services.ai_seller import _seller_is_question

    assert _seller_is_question("Quanto tempo demora a instalação?")
    assert _seller_is_question("e se eu mudar de endereço")
    assert not _seller_is_question("posso pagar no boleto?")
    assert not _seller_is_question("sim, pode seguir?")
    assert not _seller_is_question("Rafael Santos")
    assert not _seller_is_question("10")


def test_seller_question_reply_answers_from_kb_and_resumes_step(monkeypatch):
    from app.services import ai_runtime, ai_seller

    captured = {}

    def fake_run(agent, prompt):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output=ai_seller.SellerKnowledgeAnswer(
            answered=True,
            answer="O prazo contratual de instalação é de até 45 dias úteis.",
        ))

    monkeypatch.setattr(ai_runtime, "run_agent_sync", fake_run)
    monkeypatch.setattr(ai_seller, "_seller_knowledge", lambda db, agent, query="": "- Condições contratuais FTTH: até 45 dias úteis")
    monkeypatch.setattr(ai_seller, "settings", replace(ai_seller.settings, openai_api_key="sk-test"))
    state = _phase4_state(step="BILLING")
    agent = SimpleNamespace(name="Vendedor", system_prompt="", model="m", reasoning_effort="low")

    reply = ai_seller._seller_question_reply(
        None, agent=agent, state=state, collected={}, text="Quanto tempo demora a instalação?",
    )

    assert reply.startswith("O prazo contratual de instalação é de até 45 dias úteis.")
    assert "forma de pagamento" in reply
    assert "45 dias úteis" in captured["prompt"]


def test_seller_question_reply_never_invents_when_kb_has_no_answer(monkeypatch):
    from app.services import ai_runtime, ai_seller

    monkeypatch.setattr(
        ai_runtime,
        "run_agent_sync",
        lambda agent, prompt: SimpleNamespace(final_output=ai_seller.SellerKnowledgeAnswer(answered=False)),
    )
    monkeypatch.setattr(ai_seller, "_seller_knowledge", lambda db, agent, query="": "- item")
    monkeypatch.setattr(ai_seller, "settings", replace(ai_seller.settings, openai_api_key="sk-test"))
    state = _phase4_state(step="BILLING")
    agent = SimpleNamespace(name="Vendedor", system_prompt="", model="m", reasoning_effort="low")

    reply = ai_seller._seller_question_reply(
        None, agent=agent, state=state, collected={}, text="Vocês têm sinal de TV a cabo?",
    )

    assert "não tenho confirmada" in reply
    assert "forma de pagamento" in reply


def test_seed_gives_closer_its_own_knowledge_base():
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.main import app
    from app.models import AIAgentProfile, AIKnowledgeItem

    with TestClient(app):
        db = SessionLocal()
        try:
            closer = db.scalar(select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-vendedor-fechamento"))
            assert closer is not None
            titles = set(db.scalars(
                select(AIKnowledgeItem.title).where(AIKnowledgeItem.agent_id == closer.id)
            ))
            assert "Condições contratuais FTTH" in titles
            assert "Fechamento digital - resumo, aceite e assinatura" in titles
            assert "Formas de pagamento e fatura no canal digital" in titles
        finally:
            db.close()
