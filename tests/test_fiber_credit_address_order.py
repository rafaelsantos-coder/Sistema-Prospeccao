from types import SimpleNamespace

from app.services import ai_seller as seller


def test_approved_fiber_requests_cep_before_other_playbook_questions():
    state = SimpleNamespace(step="CPF", plan_label="Fibra 650 Mega")
    collected = {"_playbook_question": "Pode mandar a localização?", "_playbook_step": "CEP"}
    question = seller._advance_with_playbook(None, state, collected, fallback_step="CEP")
    assert state.step == "CEP"
    assert "CEP" in question


def test_fiber_address_asks_only_missing_fields_before_checking_coverage(monkeypatch):
    state = SimpleNamespace(step="CEP", plan_label="Fibra 650 Mega")
    data = {"street": "Rua Brasil", "city": "Santo Cristo", "name": "Ana Silva"}
    assert "número" in seller._installation_address_step(None, None, state, data)
    data["number"] = "20"
    assert "bairro" in seller._installation_address_step(None, None, state, data)
    data["neighborhood"] = "Centro"
    monkeypatch.setattr(seller.feasibility, "address_point", lambda *args: None)
    assert seller._installation_address_step(None, None, state, data) == seller.feasibility.ASK_PIN
    assert state.step == "FIBER_LOCATION"
