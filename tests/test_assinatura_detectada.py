"""Assinatura do contrato detectada com o CRM no perfil legado (conversa 472, 04/11)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services import crm_sulnet
from app.services.ai_seller import SIGNED_FINAL_MESSAGE, _is_fixed_closing_text
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient, _signature_signals


def _client(opportunity: dict, *, status_route: dict | None = None):
    class _Fake(SulnetCRMClient):
        def supports(self, capability):
            return False

        def get_opportunity(self, opportunity_id):
            return opportunity

        def _request(self, method, path, **kwargs):
            if status_route is None:
                raise CRMIntegrationError("não existe", status_code=404, method=method, path=path)
            return status_route

    return object.__new__(_Fake)


def test_sinais_de_assinatura_no_payload_legado():
    assert "stage_won" in _signature_signals({"stage": {"name": "Ganho"}})
    assert "contract_signed" in _signature_signals({"contract": {"status": "Assinado"}})
    assert "contract_signed" in _signature_signals({"aiClosing": {"signedAt": "2026-11-04T17:10:00Z"}})
    assert not {"stage_won", "contract_signed"} & _signature_signals({"status": "open", "stage": "Negociação"})


def test_status_legado_reconhece_contrato_assinado(monkeypatch):
    monkeypatch.setattr(crm_sulnet, "_STATUS_ROUTE_RETRY_AT", [0.0])
    status = _client({"status": "open", "contract": {"status": "signed"}}).get_status(14153)
    assert status["contract"]["status"] == "assinado"
    assert crm_sulnet._STATUS_ROUTE_RETRY_AT[0] > 0  # /status 404: não tenta de novo a cada minuto


def test_status_legado_reconhece_ganho(monkeypatch):
    monkeypatch.setattr(crm_sulnet, "_STATUS_ROUTE_RETRY_AT", [0.0])
    status = _client({"status": "Ganho"}).get_status(14153)
    assert status["opportunity"]["status"] == "won"


def test_rota_status_usada_quando_existe(monkeypatch):
    monkeypatch.setattr(crm_sulnet, "_STATUS_ROUTE_RETRY_AT", [0.0])
    payload = {"opportunity": {"status": "won"}, "contract": {"status": "assinado"}}
    assert _client({}, status_route=payload).get_status(1) == payload


def test_mensagem_final_e_texto_fixo():
    closing = SimpleNamespace(step="COMPLETED", contract_status="assinado", sign_url="")
    assert _is_fixed_closing_text(closing, SIGNED_FINAL_MESSAGE)
    assert "agendar" in SIGNED_FINAL_MESSAGE
    assert not _is_fixed_closing_text(SimpleNamespace(step="WAITING_SIGNATURE", contract_status="", sign_url=""), SIGNED_FINAL_MESSAGE)


def test_assinatura_em_qualquer_ponto_do_status_do_crm():
    from app.services.ai_seller import _payload_shape, _signed_paths

    payload = {
        "opportunity": {"id": 14153, "status": "open"},
        "contract": {"status": "aguardando_backoffice", "signature": {"status": "signed"}},
    }
    assert _signed_paths(payload) == ["contract.signature.status"]
    assert _signed_paths({"zapsign": {"signedAt": "2026-11-04T17:10:00Z"}}) == ["zapsign.signedat"]
    assert _signed_paths({"contract": {"status": "unsigned"}}) == []
    assert _signed_paths({"contract": {"status": "pending_signature"}}) == []
    assert _signed_paths({"opportunity": {"status": "signed"}}) == []  # fora do escopo de contrato
    assert "status=open" in _payload_shape(payload)
    assert "14153" not in _payload_shape(payload)
