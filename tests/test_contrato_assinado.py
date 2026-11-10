from types import SimpleNamespace

from app.services import ai_seller


class _Client:
    def __init__(self, record=None):
        self.record = record or {}

    def get_opportunity(self, opportunity_id):
        return self.record


def _state(contract_status=""):
    return SimpleNamespace(opportunity_id=7, contract_status=contract_status, step="WAITING_SIGNATURE", completed_at=None)


def test_won_sem_assinatura_nao_conclui():
    state = _state()
    collected = {}
    out = ai_seller._complete_if_crm_already_won(
        _Client({"status": "won"}), state, collected, status_payload={"status": "won"}
    )
    assert out is None
    assert state.step == "WAITING_SIGNATURE"
    assert "_won_after_signature" not in collected


def test_won_com_assinatura_conclui():
    state = _state()
    collected = {}
    payload = {"status": "won", "contractSignatureStatus": "assinado"}
    out = ai_seller._complete_if_crm_already_won(_Client(), state, collected, status_payload=payload)
    assert out is not None
    assert state.step == "COMPLETED"


def test_won_assinatura_vem_da_oportunidade():
    state = _state()
    record = {"status": "won", "contractSignedAt": "2026-11-10T12:00:00-03:00"}
    out = ai_seller._complete_if_crm_already_won(
        _Client(record), state, {}, status_payload={"status": "won"}
    )
    assert out is not None and state.step == "COMPLETED"


def test_payload_shows_signed_outros_status_nao_valem():
    for value in ("pendente", "enviado", "visualizado", "recusado", "expirado", "erro"):
        assert not ai_seller._payload_shows_signed({"contractSignatureStatus": value})
    assert ai_seller._payload_shows_signed({"contractSignatureStatus": "assinado"})
