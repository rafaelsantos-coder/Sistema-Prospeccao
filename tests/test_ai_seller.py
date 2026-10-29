from types import SimpleNamespace

from app.services.ai_seller import (
    MOBILE_SIGNATURE_TYPE,
    _apply_mobile_answer,
    _billing_local,
    _extract_address_number,
    _extract_birth_date,
    _extract_cep,
    _extract_cpf,
    _extract_email,
    _extract_name,
    _mobile_missing_fields,
    _mobile_next_step,
    _mobile_values,
    _redact,
    _sync_plan_context,
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


def test_mobile_contract_type_rejects_m2m():
    values = _mobile_values({})
    ok, message = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "M2M", values)
    assert ok is False
    assert "Portabilidade" in message
    assert "Número novo" in message


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
