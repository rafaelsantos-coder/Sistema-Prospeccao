"""Regressões: dados fornecidos fora da ordem da pergunta."""
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import ai_seller as seller


def _row(mid, direction, body):
    return {"id": mid, "direction": direction, "body": body}


def _state(**kw):
    return SimpleNamespace(step="CPF", plan_label="Fibra 650 Mega", credit_status="", credit_analysis_id=None, opportunity_id=10, **kw)


def test_history_preserves_document_and_address_in_one_message():
    rows = [
        _row(1, "OUTBOUND", "O senhor é de qual cidade?"),
        _row(2, "INBOUND", "Santa Rosa"),
        _row(3, "OUTBOUND", "Qual seu CPF?"),
        _row(4, "INBOUND", "CPF: 529.982.247-25, CEP: 98780-000, Rua Brasil, 20, Bairro Centro"),
        _row(5, "OUTBOUND", "Qual o CEP?"),
        _row(6, "INBOUND", "Quero contratar"),
    ]
    data = seller._historical_closing_fields(rows)
    assert data["cpf"] == "52998224725"
    assert data["cep"] == "98780000"
    assert data["city"] == "Santa Rosa"
    assert data["street"] == "Rua Brasil"
    assert data["number"] == "20"
    assert data["neighborhood"] == "Centro"


def test_phone_and_outbound_data_do_not_become_cpf():
    assert "cpf" not in seller._historical_closing_fields([
        _row(1, "OUTBOUND", "Seu CPF seria 529.982.247-25?"),
        _row(2, "INBOUND", "Meu telefone é 55999999999"),
    ])


def test_document_change_clears_prior_credit_and_address_change_clears_nap():
    state = _state()
    state.credit_status = "APROVADO"
    state.credit_analysis_id = 5
    data = {"cpf": "52998224725", "cep": "98780000", "street": "Rua Brasil", "number": "20",
            "fiberFeasibility": {"status": "available", "nap": {"id": 1}}, "_closing_history_cursor": 1}
    seller._reconcile_closing_history(state, data, [
        _row(2, "INBOUND", "CPF: 123.456.789-09, CEP: 98790-000"),
    ], current_id=2)
    assert data["cpf"] == "12345678909"
    assert not state.credit_status
    assert state.credit_analysis_id is None
    assert "fiberFeasibility" not in data
    assert "number" not in data


def test_verified_address_requests_only_missing_cpf(monkeypatch):
    state = _state()
    data = {"street": "Rua Brasil", "number": "20", "neighborhood": "Centro", "city": "Santa Rosa",
            "_checked_fiber_address": "Rua Brasil, 20, Santa Rosa",
            "fiberFeasibility": {"status": "available", "nap": {"id": 1}}}
    monkeypatch.setattr(seller.feasibility, "address_point", Mock(side_effect=AssertionError("duplicate lookup")))
    reply = seller._installation_address_step(None, None, state, data)
    assert state.step == "CPF"
    assert "CPF" in reply


def test_known_document_consulted_without_asking_again(monkeypatch):
    state = _state()
    data = {"cpf": "52998224725"}
    client = Mock()
    client.consult_credit.return_value = {"status": "APROVADO", "approved": True, "analysisId": 1}
    monkeypatch.setattr(seller, "_adopt_name_from_crm", lambda *a: None)
    monkeypatch.setattr(seller, "_after_approved_document", lambda *a: "Qual é o CEP?")
    assert seller._consult_known_document(None, client, state, data, None) == "Qual é o CEP?"
    client.consult_credit.assert_called_once_with(10, "52998224725")
    assert seller._consult_known_document(None, client, state, data, None) == "Qual é o CEP?"
    assert client.consult_credit.call_count == 1
