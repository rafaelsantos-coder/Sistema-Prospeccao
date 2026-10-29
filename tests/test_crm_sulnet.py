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
