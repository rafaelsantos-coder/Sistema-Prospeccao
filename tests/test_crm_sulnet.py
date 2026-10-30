from __future__ import annotations

from typing import Any

import app.services.crm_sulnet as crm_module
from app.services.crm_sulnet import CRMPlaybookMissingItem, SulnetCRMClient


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
        if url.endswith("/api/capture/config"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif url.endswith("/api/capture/context"):
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
    config = client.config()
    assert config["originId"] == 77
    assert config["apiVersion"] == 1
    assert config["legacy"] is True

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
                "productInfo": {
                    "categoryForm": {
                        "values": {
                            "vencimento": 10,
                            "forma_pagamento": "Boleto",
                        },
                        "files": {
                            "documento_existente": {
                                "name": "arquivo.pdf",
                            }
                        },
                    }
                },
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
    assert patch["json"]["productInfo"]["categoryForm"]["categoryId"] == 13
    assert patch["json"]["productInfo"]["aiClosing"]["paymentMethod"] == "Boleto"
    values = patch["json"]["productInfo"]["categoryForm"]["values"]
    assert values["vencimento"] == 10
    assert values["forma_pagamento"] == "Boleto"
    assert values["tipo_contratacao"] == "Número novo"
    assert values["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert patch["json"]["productInfo"]["categoryForm"]["files"] == {
        "documento_existente": {"name": "arquivo.pdf"}
    }



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
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": dict(self.headers),
            }
        )
        response = FakeResponse()
        if method == "GET" and url.endswith("/api/capture/config"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif method == "POST" and url.endswith("/api/capture/leads/1234/win"):
            response.status_code = 404
            response.payload = {"error": "Rota não encontrada."}
        elif method == "GET" and url.endswith("/api/capture/context"):
            response.status_code = 200
            response.payload = {"originId": 77}
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
    assert "stageId" not in patch["json"]
    assert patch["headers"]["Authorization"] == "Bearer service-token-value"
    assert "Idempotency-Key" in patch["headers"]
    assert "X-Integration-Key" not in patch["headers"]
    assert result["opportunity"]["status"] == "won"
    assert result["backofficeOpportunityId"] == 9001
    assert result["contract"]["status"] == "aguardando_backoffice"



def test_capture_api_does_not_use_integration_key_as_bearer(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        integration_key="integration-test",
    )

    try:
        client.config()
    except crm_module.CRMIntegrationError as exc:
        assert exc.error_code == "CRM_BEARER_NOT_CONFIGURED"
    else:
        raise AssertionError("Capture API deve exigir autenticação Bearer.")



class CapabilityV2Client:
    captured: list[dict[str, Any]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "CapabilityV2Client":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "params": kwargs.get("params"),
            }
        )
        response = FakeResponse()
        if method == "GET" and url.endswith("/api/capture/config"):
            response.payload = {
                "apiVersion": 2,
                "appVersion": "v-test",
                "capabilities": {
                    "products": True,
                    "leadUpdate": True,
                    "markWon": True,
                    "status": True,
                    "contract": True,
                },
                "stages": [
                    {"id": 110, "name": "DADOS E PLANO"},
                    {"id": 120, "name": "GANHO"},
                ],
            }
        elif method == "GET" and url.endswith("/api/catalog/products"):
            response.payload = [
                {
                    "id": 501,
                    "name": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26",
                    "monthlyValue": 69.80,
                    "categoryId": 83,
                    "categoryName": "TELEFONIA MÓVEL",
                }
            ]
        elif method == "GET" and url.endswith("/api/capture/products"):
            response.payload = [
                {"id": 501, "name": "Sulmóvel 15GB", "monthlyValue": 69.80}
            ]
        elif method == "PATCH" and url.endswith("/api/capture/leads/1234"):
            response.payload = {"opportunityId": 1234, "ok": True}
        elif method == "POST" and url.endswith("/api/capture/leads/1234/win"):
            response.payload = {
                "opportunity": {"opportunityId": 1234, "status": "won"},
                "contract": {"status": "aguardando_backoffice"},
            }
        elif method == "GET" and url.endswith("/api/capture/leads/1234/status"):
            response.payload = {
                "opportunity": {"opportunityId": 1234, "status": "won"},
                "contract": {"status": "aguardando_backoffice"},
            }
        elif method == "POST" and url.endswith("/api/capture/leads/1234/contract"):
            response.payload = {
                "status": "aguardando_assinatura",
                "signUrl": "https://sign.example/123",
            }
        return response


def test_v2_profile_uses_capture_contract_routes(monkeypatch):
    CapabilityV2Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", CapabilityV2Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="b" * 32,
        integration_key="x" * 32,
    )

    profile = client.integration_profile()
    assert profile["apiVersion"] == 2
    assert profile["legacy"] is False
    assert client.products("15GB")[0]["id"] == 501
    assert client.update_opportunity(
        1234,
        {"clientEmail": "cliente@exemplo.com"},
    )["ok"] is True
    assert client.mark_won(1234)["contract"]["status"] == "aguardando_backoffice"
    assert client.get_status(1234)["opportunity"]["status"] == "won"
    assert client.start_contract(1234)["signUrl"] == "https://sign.example/123"

    urls = [item["url"] for item in CapabilityV2Client.captured]
    assert any("/api/catalog/products" in url for url in urls)
    assert not any("/api/capture/products" in url for url in urls)
    assert not any("/api/opportunities/1234" in url for url in urls)
    assert any(url.endswith("/api/capture/leads/1234/win") for url in urls)


def test_legacy_profile_avoids_blind_win_and_contract_calls(monkeypatch):
    WinFallbackClient.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", WinFallbackClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="b" * 32,
    )
    result = client.mark_won(1234)

    assert result["opportunity"]["status"] == "won"
    urls = [item["url"] for item in WinFallbackClient.captured]
    assert not any(url.endswith("/api/capture/leads/1234/win") for url in urls)
    assert not any(url.endswith("/api/capture/leads/1234/contract") for url in urls)



class Phase1Client:
    captured: list[dict[str, Any]] = []

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = dict(headers)

    def __enter__(self) -> "Phase1Client":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.__class__.captured.append(
            {
                "method": method,
                "url": url,
                "headers": dict(self.headers),
                "json": kwargs.get("json"),
                "params": kwargs.get("params"),
            }
        )
        response = FakeResponse()
        if url.endswith("/api/capture/config"):
            response.payload = {
                "apiVersion": 3,
                "capabilities": {
                    "markWon": True,
                    "agentContractGenerate": True,
                },
            }
        elif "/playbook/templates" in url:
            response.payload = {
                "category": "telefonia-movel",
                "items": [
                    {
                        "code": "tipo_contratacao",
                        "question": "Seu chip será linha nova, portabilidade ou M2M?",
                    }
                ],
            }
        elif "/playbook" in url:
            response.payload = {
                "opportunityId": 1234,
                "protocol": "CRM-TEST-1234",
                "nextStep": "email",
                "nextQuestion": "",
                "readyForWin": False,
                "sdrIaFunnel": True,
                "sdrIaDefaults": {
                    "vencimento": "10",
                    "taxa_ativacao": "Não",
                    "tipo_assinatura_contrato": "Assinatura Digital IA",
                },
                "sdrIaFieldPolicy": {
                    "doNotAsk": ["vencimento", "novo_iccid", "m2m_iccids"],
                    "stillAsk": ["forma_pagamento", "envio_fatura"],
                },
                "credit": {
                    "status": "Aprovado",
                    "approved": True,
                    "rejected": False,
                },
                "missing": [
                    {
                        "code": "clientEmail",
                        "label": "E-mail",
                        "question": "Qual seu e-mail?",
                        "group": "cliente",
                        "patchKey": "clientEmail",
                    },
                    {
                        "code": "vencimento",
                        "label": "Vencimento",
                        "question": "Qual dia de vencimento?",
                        "group": "faturamento",
                        "patchKey": "productInfo.categoryForm.values.vencimento",
                        "suggestedValue": "10",
                    },
                ],
            }
        elif url.endswith("/api/capture/leads/1234/win"):
            response.payload = {
                "opportunity": {"opportunityId": 1234, "status": "won"},
                "contract": {"status": "aguardando_backoffice"},
            }
        elif url.endswith("/api/integracoes/agent/v1/opportunities/1234/contract/generate"):
            response.payload = {
                "pdfUrl": "https://crm.example/contracts/1234.pdf",
                "templateId": "tpl-800",
                "protocol": "CRM-TEST-1234",
            }
        elif url.endswith("/webhook/contrato"):
            response.payload = {"ok": True}
        else:
            response.payload = {"ok": True}
        return response


def test_playbook_is_normalized_and_uses_bearer_only(monkeypatch):
    Phase1Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", Phase1Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
        integration_key="integration-test",
    )
    playbook = client.playbook(1234, category="Telefonia Móvel")

    assert playbook.opportunity_id == 1234
    assert playbook.protocol == "CRM-TEST-1234"
    assert playbook.category == "telefonia-movel"
    assert playbook.credit_approved is True
    assert playbook.ready_for_win is False
    assert playbook.sdr_ia_funnel is True
    assert playbook.sdr_ia_defaults["vencimento"] == "10"
    assert playbook.sdr_ia_defaults["taxa_ativacao"] == "Não"
    assert playbook.sdr_ia_defaults["tipo_assinatura_contrato"] == "Assinatura Digital IA"
    assert "vencimento" in playbook.sdr_ia_field_policy["doNotAsk"]
    assert playbook.next_question == "Qual seu e-mail?"
    assert playbook.missing_codes == ["clientEmail", "vencimento"]
    assert playbook.missing[0].patch_key == "clientEmail"
    assert playbook.missing[1].suggested_value == "10"

    request = next(
        item for item in Phase1Client.captured
        if "/opportunities/1234/playbook" in item["url"]
    )
    assert request["params"] == {"category": "telefonia-movel"}
    assert request["headers"]["Authorization"] == "Bearer bearer-test"
    assert "X-Integration-Key" not in request["headers"]


def test_playbook_template_normalizes_mobile_category(monkeypatch):
    Phase1Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", Phase1Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
    )
    template = client.playbook_template(category="Sulmóvel")

    assert template["category"] == "telefonia-movel"
    assert template["items"][0]["code"] == "tipo_contratacao"
    request = next(
        item for item in Phase1Client.captured
        if "/playbook/templates" in item["url"]
    )
    assert request["params"] == {"category": "telefonia-movel"}


def test_mark_won_sends_idempotency_key_with_bearer(monkeypatch):
    Phase1Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", Phase1Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
        integration_key="integration-test",
    )
    result = client.mark_won(
        1234,
        idempotency_key="123e4567-e89b-42d3-a456-426614174000",
    )

    assert result["opportunity"]["status"] == "won"
    request = next(
        item for item in Phase1Client.captured
        if item["url"].endswith("/api/capture/leads/1234/win")
    )
    assert request["headers"]["Authorization"] == "Bearer bearer-test"
    assert request["headers"]["Idempotency-Key"] == "123e4567-e89b-42d3-a456-426614174000"
    assert "X-Integration-Key" not in request["headers"]


def test_contract_webhook_uses_only_integration_key(monkeypatch):
    Phase1Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", Phase1Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
        integration_key="integration-test",
    )
    result = client.send_contract_webhook(
        protocol="CRM-TEST-1234",
        file_name="contrato.pdf",
        pdf_base64="TEST-PDF",
    )

    assert result["ok"] is True
    request = next(
        item for item in Phase1Client.captured
        if item["url"].endswith("/webhook/contrato")
    )
    assert request["headers"]["X-Integration-Key"] == "integration-test"
    assert "Authorization" not in request["headers"]


def test_agent_contract_generate_uses_official_payload(monkeypatch):
    Phase1Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", Phase1Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
    )
    result = client.generate_agent_contract(1234)

    assert result["templateId"] == "tpl-800"
    request = next(
        item for item in Phase1Client.captured
        if item["url"].endswith(
            "/api/integracoes/agent/v1/opportunities/1234/contract/generate"
        )
    )
    assert request["headers"]["Authorization"] == "Bearer bearer-test"
    assert request["json"] == {
        "sendSignature": True,
        "requireReadyForWin": True,
    }



class SanitizedErrorClient:
    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.headers = headers

    def __enter__(self) -> "SanitizedErrorClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        response = FakeResponse()
        response.status_code = 422
        response.payload = {
            "error": "Documento 11111111111 inválido para a@b.co",
            "code": "VALIDATION_ERROR",
            "clientCpf": "11111111111",
            "clientEmail": "a@b.co",
            "safeField": "faltando",
        }
        return response


def test_crm_error_exposes_only_sanitized_diagnostics(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", SanitizedErrorClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
    )
    try:
        client.playbook(1234, category="fibra")
    except crm_module.CRMIntegrationError as exc:
        assert exc.status_code == 422
        assert exc.method == "GET"
        assert "/playbook" in exc.path
        assert exc.error_code == "VALIDATION_ERROR"
        assert "11111111111" not in str(exc)
        assert "a@b.co" not in str(exc)
        assert exc.response_body["clientCpf"] == "[REDACTED]"
        assert exc.response_body["clientEmail"] == "[REDACTED]"
        assert exc.response_body["safeField"] == "faltando"
        assert exc.as_dict()["statusCode"] == 422
    else:
        raise AssertionError("Era esperado CRMIntegrationError.")



def test_agent_contract_generate_attempts_route_even_before_capability_profile_updates(monkeypatch):
    CapabilityV2Client.captured = []
    monkeypatch.setattr(crm_module.httpx, "Client", CapabilityV2Client)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="bearer-test",
    )
    client.generate_agent_contract(1234)

    request = next(
        item for item in CapabilityV2Client.captured
        if "/contract/generate" in item["url"]
    )
    assert request["json"] == {
        "sendSignature": True,
        "requireReadyForWin": True,
    }



def test_playbook_missing_item_parses_dynamic_options():
    item = CRMPlaybookMissingItem.from_payload(
        {
            "code": "cf_nova_regra",
            "label": "Nova regra",
            "question": "Escolha uma opção",
            "patchKey": "customFields.cf_nova_regra",
            "options": [
                {"id": "a", "label": "Opção A", "value": "A"},
                {"id": "b", "label": "Opção B", "value": "B"},
            ],
        }
    )

    assert item is not None
    assert item.patch_key == "customFields.cf_nova_regra"
    assert item.options == [
        {"id": "a", "label": "Opção A", "value": "A"},
        {"id": "b", "label": "Opção B", "value": "B"},
    ]


def test_playbook_patch_payload_builds_nested_payload():
    payload = SulnetCRMClient._playbook_patch_payload(
        "productInfo.categoryForm.values.tipo_contratacao",
        "Número novo",
    )

    assert payload == {
        "productInfo": {
            "categoryForm": {
                "values": {
                    "tipo_contratacao": "Número novo",
                }
            }
        }
    }
