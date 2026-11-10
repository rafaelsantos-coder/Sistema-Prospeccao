"""Webhook da ZapSign marca o contrato como assinado (conversa 472, 04/11)."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import AIClosingState
from app.routers import zapsign
from app.services import crm_sulnet

SIGNER = "18103a1a-19d8-477c-9fb6-3c19c512429b"
PAYLOAD = {
    "event_type": "doc_signed",
    "token": "aaaaaaaa-1111-2222-3333-444444444444",
    "status": "signed",
    "signers": [{"token": SIGNER, "status": "signed", "sign_url": f"https://app.zapsign.com.br/verificar/{SIGNER}"}],
}


def test_documento_assinado():
    assert zapsign.zapsign_document_signed(PAYLOAD)
    assert not zapsign.zapsign_document_signed({"event_type": "doc_created", "status": "pending"})
    assert zapsign.zapsign_document_signed({"signers": [{"status": "signed"}]})


def test_webhook_exige_segredo_e_marca_fechamento(monkeypatch):
    monkeypatch.setattr(zapsign, "settings", replace(zapsign.settings, zapsign_webhook_secret="segredo-de-teste-123"))
    with TestClient(app) as client:
        assert client.post("/webhooks/zapsign/errado", json=PAYLOAD).status_code == 404
        db = SessionLocal()
        try:
            state = db.query(AIClosingState).filter(AIClosingState.sign_url != "").first()
            if state is None:
                return
            state.step = "WAITING_SIGNATURE"
            state.sign_url = f"https://app.zapsign.com.br/verificar/{SIGNER}"
            state.contract_status = "pendente"
            db.commit()
            response = client.post("/webhooks/zapsign/segredo-de-teste-123", json=PAYLOAD)
            assert response.status_code == 200
            db.refresh(state)
            assert state.contract_status == "assinado"
        finally:
            db.close()


def test_mark_contracts_signed_sem_banco():
    class _DB:
        def __init__(self, states):
            self.states = states

        def scalars(self, _stmt):
            return self.states

    match = type("S", (), {"id": 9, "step": "WAITING_SIGNATURE", "sign_url": f"https://app.zapsign.com.br/verificar/{SIGNER}", "contract_status": ""})()
    other = type("S", (), {"id": 4, "step": "WAITING_SIGNATURE", "sign_url": "https://app.zapsign.com.br/verificar/bbbbbbbb-1111-2222-3333-444444444444", "contract_status": ""})()
    assert zapsign.mark_contracts_signed(_DB([match, other]), PAYLOAD) == [9]
    assert match.contract_status == "assinado" and other.contract_status == ""


def test_status_do_crm_sem_contrato_cai_no_legado(monkeypatch):
    class _Fake(crm_sulnet.SulnetCRMClient):
        def supports(self, capability):
            return False

        def get_opportunity(self, opportunity_id):
            return {"status": "won"}

        def _request(self, method, path, **kwargs):
            return {"lead": {"id": 1, "creditStatus": "Aprovado"}}

    monkeypatch.setattr(crm_sulnet, "_STATUS_ROUTE_RETRY_AT", [0.0])
    status = object.__new__(_Fake).get_status(14153)
    assert status["opportunity"]["status"] == "won"


def test_lead_404_depois_da_assinatura_nao_trava():
    from types import SimpleNamespace

    from app.services.ai_seller import _complete_if_crm_already_won
    from app.services.crm_sulnet import CRMIntegrationError

    class _Client:
        def get_status(self, opportunity_id):
            raise CRMIntegrationError("Lead não encontrada.", status_code=404)

    state = SimpleNamespace(opportunity_id=14153, step="WAITING_SIGNATURE")
    assert _complete_if_crm_already_won(_Client(), state, {}) is None
