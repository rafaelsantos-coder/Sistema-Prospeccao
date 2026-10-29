from app.services.ai_seller import (
    _billing_local,
    _extract_address_number,
    _extract_cep,
    _extract_cpf,
    _extract_email,
    _extract_name,
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
