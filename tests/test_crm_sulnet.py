from __future__ import annotations

from typing import Any

import app.services.crm_sulnet as crm_module
from app.services.crm_sulnet import SulnetCRMClient


class FakeResponse:
    status_code = 200
    payload: dict[str, Any] = {"ok": True}

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    captured_headers: dict[str, str] | None = None
    captured_json: dict[str, Any] | None = None
    captured_url: str | None = None

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.__class__.captured_headers = headers

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured_url = url
        self.__class__.captured_json = kwargs.get("json")
        response = FakeResponse()
        if url.endswith("/api/capture/context"):
            response.payload = {"originId": 77}
        elif url.endswith("/api/capture/leads"):
            response.payload = {"lead": {"id": 1234, "protocol": "CRM-2026-1234"}}
        elif url.endswith("/api/capture/leads/1234/consult"):
            response.payload = {
                "analysis": {
                    "id": 88,
                    "status": "Aprovado",
                    "approved": True,
                    "rejected": False,
                    "canContest": False,
                }
            }
        else:
            response.payload = {"ok": True}
        return response


def test_crm_capture_uses_service_bearer_and_not_integration_key(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    assert client.config() == {"originId": 77}

    headers = FakeClient.captured_headers or {}
    assert headers["Authorization"] == "Bearer service-token-value"
    assert "X-Integration-Key" not in headers



def test_create_lead_normalizes_onprem_response_and_disables_auto_consult(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    result = client.create_opportunity(
        {
            "clientName": "Rafael Teste",
            "clientPhone": "55999002508",
        }
    )

    assert result["opportunityId"] == 1234
    assert result["protocol"] == "CRM-2026-1234"
    assert FakeClient.captured_json is not None
    assert FakeClient.captured_json["runConsult"] is False
    assert FakeClient.captured_json["originId"] == 77



def test_consult_credit_sends_birth_date_without_persisting_cpf_locally(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    result = client.consult_credit(
        1234,
        "00380171058",
        birth_date="1990-05-10",
    )

    assert FakeClient.captured_json == {
        "clientCpf": "00380171058",
        "clientBirthDate": "1990-05-10",
    }
    assert result["analysisId"] == 88
    assert result["approved"] is True
    assert result["rejected"] is False
    assert result["canContest"] is False


def test_consult_credit_can_send_birth_date_after_cpf_is_already_in_crm(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    client.consult_credit(1234, birth_date="1990-05-10")

    assert FakeClient.captured_json == {
        "clientBirthDate": "1990-05-10",
    }



class CategoryFormClient:
    captured: list[dict[str, Any]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "CategoryFormClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append(
            {"method": method, "url": url, "json": kwargs.get("json")}
        )
        response = FakeResponse()
        if method == "GET" and url.endswith("/api/capture/leads/1234"):
            response.payload = {
                "opportunityId": 1234,
                "aiClosing": {"paymentMethod": "Boleto", "dueDay": 10},
            }
        elif method == "PATCH" and url.endswith("/api/opportunities/1234"):
            response.payload = {"id": 1234, "ok": True}
        else:
            response.payload = {"ok": True}
        return response


def test_update_category_form_uses_generic_opportunity_patch(monkeypatch):
    CategoryFormClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", CategoryFormClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    result = client.update_category_form(
        1234,
        {
            "tipo_contratacao": "Número novo",
            "tipo_assinatura_contrato": "Assinatura Digital IA",
        },
        category_id=13,
        plan_id=44,
    )

    assert result["ok"] is True
    patch = next(
        item
        for item in CategoryFormClient.captured
        if item["method"] == "PATCH"
    )
    assert patch["url"].endswith("/api/opportunities/1234")
    assert patch["json"]["categoryId"] == 13
    assert patch["json"]["planIds"] == [44]
    assert patch["json"]["productInfo"]["aiClosing"]["paymentMethod"] == "Boleto"
    values = patch["json"]["productInfo"]["categoryForm"]["values"]
    assert values["tipo_contratacao"] == "Número novo"
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert patch["json"]["productInfo"]["categoryForm"]["files"] == {}



class CatalogFallbackClient:
    captured: list[tuple[str, str]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "CatalogFallbackClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append((method, url))
        response = FakeResponse()
        if url.endswith("/api/capture/products"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif url.endswith("/api/catalog/products"):
            response.status_code = 200
            response.payload = [
                {
                    "category_id": 13,
                    "category_name": "TELEFONIA MÓVEL",
                    "products": [
                        {
                            "id": 501,
                            "name": "Sulmóvel 15GB",
                            "monthlyValue": 69.80,
                        }
                    ],
                }
            ]
        return response


def test_products_falls_back_to_catalog_and_flattens_grouped_response(monkeypatch):
    CatalogFallbackClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", CatalogFallbackClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    products = client.products("Sulmovel 15GB")

    assert len(products) == 1
    assert products[0]["id"] == 501
    assert products[0]["categoryId"] == 13
    assert products[0]["categoryName"] == "TELEFONIA MÓVEL"
    assert any(url.endswith("/api/catalog/products") for _, url in CatalogFallbackClient.captured)
    assert not any(url.endswith("/api/capture/products") for _, url in CatalogFallbackClient.captured)



class PlanDisambiguationClient:
    captured: list[dict[str, Any]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "PlanDisambiguationClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append(
            {"method": method, "url": url, "json": kwargs.get("json")}
        )
        response = FakeResponse()
        if method == "PATCH" and url.endswith("/api/capture/leads/1234"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif method == "GET" and url.endswith("/api/capture/leads/1234"):
            response.status_code = 200
            response.payload = {"opportunityId": 1234, "aiClosing": {}}
        elif method == "GET" and url.endswith("/api/capture/products"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif method == "GET" and url.endswith("/api/catalog/products"):
            response.status_code = 200
            response.payload = [
                {
                    "category_id": 83,
                    "category_name": "TELEFONIA MÓVEL",
                    "products": [
                        {"id": 392, "name": "Meu Trabalho Comigo 15GB", "monthlyValue": 69.80},
                        {"id": 393, "name": "Meu Trabalho Comigo 15GB Plus", "monthlyValue": 79.80},
                    ],
                }
            ]
        elif method == "GET" and url.endswith("/api/capture/context"):
            response.status_code = 200
            response.payload = {"stages": [{"id": 110, "name": "DADOS E PLANO"}]}
        elif method == "PATCH" and url.endswith("/api/opportunities/1234"):
            response.status_code = 200
            response.payload = {"id": 1234}
        return response


def test_plan_resolution_uses_expected_value_only_as_disambiguation(monkeypatch):
    PlanDisambiguationClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", PlanDisambiguationClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    client.update_opportunity(
        1234,
        {"planLabel": "Sulmóvel 15GB", "monthlyValue": 69.80},
    )

    patch = next(
        item
        for item in PlanDisambiguationClient.captured
        if item["method"] == "PATCH"
        and item["url"].endswith("/api/opportunities/1234")
    )
    assert patch["json"]["planIds"] == [392]
    assert patch["json"]["categoryId"] == 83
    assert patch["json"]["monthlyValue"] == 69.80


def test_update_opportunity_prefers_generic_route_without_capture_404(monkeypatch):
    PlanDisambiguationClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", PlanDisambiguationClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    client.update_opportunity(
        1234,
        {"clientEmail": "cliente@exemplo.com"},
    )

    patch_urls = [
        item["url"]
        for item in PlanDisambiguationClient.captured
        if item["method"] == "PATCH"
    ]
    assert any(url.endswith("/api/opportunities/1234") for url in patch_urls)
    assert not any(url.endswith("/api/capture/leads/1234") for url in patch_urls)



class WinFallbackClient:
    captured: list[dict[str, Any]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "WinFallbackClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append(
            {"method": method, "url": url, "json": kwargs.get("json")}
        )
        response = FakeResponse()
        if method == "POST" and url.endswith("/api/capture/leads/1234/win"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif method == "GET" and url.endswith("/api/capture/context"):
            response.status_code = 200
            response.payload = {
                "stages": [
                    {"id": 110, "name": "DADOS E PLANO"},
                    {"id": 120, "name": "GANHO"},
                ]
            }
        elif method == "PATCH" and url.endswith("/api/opportunities/1234"):
            response.status_code = 200
            response.payload = {
                "id": 1234,
                "status": "won",
                "backofficeCopy": {"id": 9001},
            }
        elif method == "GET" and url.endswith("/api/capture/leads/1234/status"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        return response


def test_mark_won_falls_back_to_generic_opportunity_when_capture_route_missing(monkeypatch):
    WinFallbackClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", WinFallbackClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    result = client.mark_won(1234)

    patch = next(
        item
        for item in WinFallbackClient.captured
        if item["method"] == "PATCH"
        and item["url"].endswith("/api/opportunities/1234")
    )
    assert patch["json"]["status"] == "won"
    assert patch["json"]["stageId"] == 120
    assert result["opportunity"]["status"] == "won"
    assert result["backofficeOpportunityId"] == 9001
    assert result["contract"]["status"] == "aguardando_backoffice"
