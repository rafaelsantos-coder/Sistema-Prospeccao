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



def test_mobile_contract_type_and_new_number_flow():
    values = _mobile_values({})
    assert values["tipo_assinatura_contrato"] == MOBILE_SIGNATURE_TYPE

    ok, _ = _apply_mobile_answer("MOBILE_CONTRACT_TYPE", "número novo", values)
    assert ok is True
    assert values["tipo_contratacao"] == "Número novo"
    assert _mobile_next_step(values) == "MOBILE_NEW_ICCID"

    ok, _ = _apply_mobile_answer("MOBILE_NEW_ICCID", "8955000000000000001", values)
    assert ok is True
    assert _mobile_next_step(values) == "MOBILE_NEW_NUMBER"

    ok, _ = _apply_mobile_answer("MOBILE_NEW_NUMBER", "55 55 99999-0000", values)
    assert ok is True
    assert _mobile_next_step(values) == "MOBILE_DUE_DAY"


def test_mobile_portability_requires_all_visible_fields():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número Portabilidade",
        "port_iccid": "8955000000000000001",
        "numero_portado": "5555999990000",
        "port_titular": "Sim",
    }
    missing = _mobile_missing_fields(values)
    assert "port_nome_titular" in missing
    assert "port_cpf_cnpj" in missing
    assert "port_data" in missing


def test_mobile_boleto_billing_requires_delivery_and_unification_reason():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número novo",
        "novo_iccid": "8955000000000000001",
        "numero_novo": "5555999990000",
        "vencimento": 10,
        "taxa_ativacao": "Não",
        "forma_pagamento": "Boleto",
        "unificar_faturas": "Não",
    }
    missing = _mobile_missing_fields(values)
    assert "envio_fatura" in missing
    assert "unificar_motivo" in missing

    values["envio_fatura"] = ["WhatsApp", "E-mail"]
    values["unificar_motivo"] = "Faturas separadas"
    assert _mobile_missing_fields(values) == []


def test_mobile_debit_requires_bank_details():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "Número novo",
        "novo_iccid": "8955000000000000001",
        "numero_novo": "5555999990000",
        "vencimento": 15,
        "taxa_ativacao": "Não",
        "forma_pagamento": "Débito em Conta",
        "unificar_faturas": "Sim",
    }
    missing = _mobile_missing_fields(values)
    assert "debito_banco" in missing
    assert "debito_agencia" in missing
    assert "debito_conta" in missing
    assert "debito_titular" in missing


def test_mobile_m2m_requires_one_iccid_per_chip():
    values = {
        "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
        "tipo_contratacao": "M2M",
        "m2m_quantidade": 2,
        "m2m_iccids": "8955000000000000001",
        "vencimento": 10,
        "taxa_ativacao": "Não",
        "forma_pagamento": "Boleto",
        "envio_fatura": ["E-mail"],
        "unificar_faturas": "Sim",
    }
    assert "m2m_iccids" in _mobile_missing_fields(values)

    values["m2m_iccids"] = "8955000000000000001\n8955000000000000002"
    assert _mobile_missing_fields(values) == []
