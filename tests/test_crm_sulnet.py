from __future__ import annotations

from typing import Any

import app.services.crm_sulnet as crm_module
from app.services.crm_sulnet import SulnetCRMClient


class FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"ok": True}


class FakeClient:
    captured_headers: dict[str, str] | None = None

    def __init__(self, *, headers: dict[str, str], **_: Any):
        self.__class__.captured_headers = headers

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, *_: Any, **__: Any) -> FakeResponse:
        return FakeResponse()


def test_crm_capture_uses_service_bearer_and_not_integration_key(monkeypatch):
    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)

    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value",
    )
    assert client.config() == {"ok": True}

    headers = FakeClient.captured_headers or {}
    assert headers["Authorization"] == "Bearer service-token-value"
    assert "X-Integration-Key" not in headers
