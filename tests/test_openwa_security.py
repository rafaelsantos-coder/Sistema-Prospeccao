"""Regressoes de seguranca do adaptador OpenWA, sem acesso a rede."""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.openwa import OpenWAClient, OpenWAConfig, OpenWAError


def _config(**changes) -> OpenWAConfig:
    data = {
        "enabled": True,
        "base_url": "https://gateway.example.net",
        "allowed_host": "gateway.example.net",
        "api_key": "owa_k1_" + "a" * 64,
        "session_id": "sessao-teste-01",
    }
    data.update(changes)
    return OpenWAConfig(**data)


@pytest.mark.parametrize("url", [
    "http://gateway.example.net",
    "https://127.0.0.1",
    "https://localhost",
    "https://gateway.example.net.evil.tld",
    "https://user:pass@gateway.example.net",
    "https://gateway.example.net/path",
    "https://gateway.example.net?access_token=secret",
    "https://gateway.example.net/#fragment",
    "file:///etc/passwd",
    "https://gateway.example.net:invalid",
])
def test_reject_unauthorized_destination(url):
    with pytest.raises(OpenWAError):
        _config(base_url=url).endpoint()


def test_private_railway_endpoint_requires_exact_host():
    conf = _config(base_url="http://openwa.railway.internal:3000",
                   allowed_host="openwa.railway.internal")
    assert conf.endpoint().endswith("/api/sessions/sessao-teste-01")


def test_disabled_and_missing_credentials_fail_closed():
    with pytest.raises(OpenWAError):
        _config(enabled=False).endpoint()
    with pytest.raises(OpenWAError):
        _config(api_key="").endpoint()
    with pytest.raises(OpenWAError):
        _config(session_id="../admin").endpoint()


def test_configuration_stays_off_by_default():
    cfg = OpenWAConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.is_configured is False


def test_only_read_only_status_endpoint_with_scoped_api_key():
    calls = []
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(
            200, json={"id": "sessao-teste-01", "status": "ready", "engineLoaded": True}
        )
    result = OpenWAClient(_config(), transport=httpx.MockTransport(handle)).status()
    assert result["status"] == "ready"
    assert result["engine_loaded"] is True
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url).endswith("/api/sessions/sessao-teste-01")
    assert calls[0].headers["x-api-key"].startswith("owa_k1_")


def test_redirects_are_not_followed():
    calls = []
    def handle(req):
        calls.append(req)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
    with pytest.raises(OpenWAError):
        OpenWAClient(_config(), transport=httpx.MockTransport(handle)).status()
    assert len(calls) == 1


def test_unexpected_session_id_rejected():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"id": "outra-sessao", "status": "ready"})
    )
    with pytest.raises(OpenWAError):
        OpenWAClient(_config(), transport=transport).status()


def test_large_payload_rejected():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps({"id": "sessao-teste-01",
                                                           "extra": "x" * 20000}))
    )
    with pytest.raises(OpenWAError):
        OpenWAClient(_config(), transport=transport).status()


def test_http_error_does_not_expose_credentials():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(401, json={"error": "secret credentials"})
    )
    with pytest.raises(OpenWAError) as error:
        OpenWAClient(_config(), transport=transport).status()
    assert "secret credentials" not in str(error.value)
    assert "owa_k1" not in str(error.value)


def test_untrusted_status_type_is_sanitized():
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, json={"id": "sessao-teste-01", "status": ["ready"],
                   "engineLoaded": "true", "privateKey": "sensitive"}
    ))
    result = OpenWAClient(_config(), transport=transport).status()
    assert result == {
        "session": "sessao-teste-01",
        "status": "desconhecido",
        "engine_loaded": False,
    }
    assert "privateKey" not in result
