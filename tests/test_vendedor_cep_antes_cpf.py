"""CEP na etapa CPF: viabilidade/cobertura antes da consulta cadastral."""

from types import SimpleNamespace

from app.services import ai_seller as seller


class _FakeCRM:
    def __init__(self, address: dict | None = None):
        self.address = address or {}
        self.updates: list[tuple[int, dict]] = []

    def lookup_cep(self, cep: str) -> dict:
        return dict(self.address)

    def update_opportunity(self, opportunity_id: int, patch: dict) -> None:
        self.updates.append((opportunity_id, dict(patch)))


def _fiber_state(**kwargs):
    data = {
        "step": "CPF",
        "plan_label": "Fibra 650 Mega",
        "opportunity_id": 99,
        "completed_at": None,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_extract_cep_from_postal_code():
    assert seller._extract_cep("91120-020") == "91120020"
    assert seller._extract_cpf("91120-020") is None


def test_cep_fora_da_cobertura_nao_pede_cpf(monkeypatch):
    state = _fiber_state()
    collected: dict = {}
    client = _FakeCRM({"city": "Porto Alegre", "uf": "RS", "cep": "91120020"})

    reply = seller._handle_cep_while_awaiting_cpf(
        None,
        client,
        state,
        collected,
        "91120-020\nPrimeiro v c tem aqui na minha região",
    )

    assert reply is not None
    assert "CPF" not in reply
    assert "cobertura" in reply.casefold() or "região" in reply.casefold()
    assert "Porto Alegre" in reply
    assert state.step == "COMPLETED"
    assert collected.get("fiberFeasibility", {}).get("status") == "out_of_coverage"


def test_cep_em_cidade_atendida_segue_endereco_sem_repetir_cpf(monkeypatch):
    state = _fiber_state()
    collected: dict = {}
    client = _FakeCRM(
        {
            "city": "Santo Ângelo",
            "uf": "RS",
            "cep": "98801000",
            "street": "Rua Exemplo",
            "neighborhood": "Centro",
        }
    )

    def _fake_install(db, crm, closing_state, data):
        closing_state.step = "ADDRESS_NUMBER"
        return "Qual é o número do imóvel?"

    monkeypatch.setattr(seller, "_installation_address_step", _fake_install)

    reply = seller._handle_cep_while_awaiting_cpf(
        None,
        client,
        state,
        collected,
        "Meu CEP é 98801-000",
    )

    assert reply == "Qual é o número do imóvel?"
    assert "11 dígitos" not in reply
    assert collected.get("cep") == "98801000"
    assert collected.get("city") == "Santo Ângelo"
    assert collected.get("_cep_before_cpf") is True
    assert state.step == "ADDRESS_NUMBER"


def test_cep_com_viabilidade_pede_cpf_somente_depois(monkeypatch):
    state = _fiber_state()
    collected: dict = {}
    client = _FakeCRM(
        {
            "city": "Santa Rosa",
            "uf": "RS",
            "cep": "98900000",
            "street": "Av Brasil",
            "neighborhood": "Centro",
            "number": "100",
        }
    )

    def _fake_install(db, crm, closing_state, data):
        data["fiberFeasibility"] = {
            "status": "available",
            "nap": {"id": 1, "name": "NAP-1", "meters": 40},
        }
        closing_state.step = "CPF"
        return "Temos disponibilidade de fibra nesse local!"

    monkeypatch.setattr(seller, "_installation_address_step", _fake_install)

    reply = seller._handle_cep_while_awaiting_cpf(
        None,
        client,
        state,
        collected,
        "98900-000",
    )

    assert "disponibilidade" in reply.casefold()
    assert "CPF" in reply
    assert state.step == "CPF"


def test_mensagem_sem_cep_na_etapa_cpf_nao_intercepta():
    state = _fiber_state()
    collected: dict = {}
    reply = seller._handle_cep_while_awaiting_cpf(
        None,
        _FakeCRM(),
        state,
        collected,
        "Primeiro v c tem aqui na minha região",
    )
    assert reply is None
