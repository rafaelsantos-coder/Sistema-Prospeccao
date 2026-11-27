from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.services.sulnet_wa import (
    GatewayClient, GatewayConfig, SulnetWAError, verify_event, valid_session_id,
)


def config(**kwargs):
    values = dict(enabled=True, base_url="http://sulnet-wa.railway.internal:3000",
                  allowed_host="sulnet-wa.railway.internal",
                  api_key="a" * 48, webhook_key="b" * 48)
    values.update(kwargs)
    return GatewayConfig(**values)


def test_gateway_is_disabled_by_default(monkeypatch):
    for key in ("SULNET_WA_ENABLED", "SULNET_WA_BASE_URL", "SULNET_WA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SulnetWAError):
        GatewayConfig.from_env().validate_target()


@pytest.mark.parametrize("url", ["https://google.com", "http://127.0.0.1:3000",
                                 "http://sulnet-wa.railway.internal.evil.com:3000",
                                 "http://user:pass@sulnet-wa.railway.internal:3000",
                                 "http://sulnet-wa.railway.internal:3000/admin",
                                 "http://sulnet-wa.railway.internal:3000?redirect=1"])
def test_private_host_allowlist_rejects_ssrf(url):
    with pytest.raises(SulnetWAError):
        config(base_url=url).validate_target()


def test_client_only_returns_safe_status_fields():
    def handler(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + "a" * 48
        return httpx.Response(200, json={"id": "abc01", "status": "ready",
                                          "phone": "5511999999999", "secret": "private"})
    session = GatewayClient(config(), transport=httpx.MockTransport(handler)).status("abc01")
    assert "secret" not in session and session["status"] == "ready"


def test_reject_cross_session_and_redirect():
    client = GatewayClient(config(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"id": "another", "status": "ready"})))
    with pytest.raises(SulnetWAError):
        client.status("abc01")
    client = GatewayClient(config(), transport=httpx.MockTransport(
        lambda _: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})))
    with pytest.raises(SulnetWAError):
        client.status("abc01")


def test_webhook_hmac_replay_timestamp_and_tamper():
    value = {"sessionId": "abc01", "messageId": "message1", "from": "5511999999999",
             "text": "Oi", "source": "SULNET_WA"}
    value["eventId"] = hashlib.sha256(b"abc01\x00message1").hexdigest()
    body = json.dumps(value).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(b"b" * 48, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert verify_event(body, stamp, sig, config())["text"] == "Oi"
    with pytest.raises(SulnetWAError):
        verify_event(body + b" ", stamp, sig, config())
    with pytest.raises(SulnetWAError):
        verify_event(body, str(int(stamp) - 900), sig, config())
    value["sessionId"] = "other"
    changed = json.dumps(value).encode()
    forged_sig = hmac.new(b"b" * 48, stamp.encode() + b"." + changed, hashlib.sha256).hexdigest()
    with pytest.raises(SulnetWAError):
        verify_event(changed, stamp, forged_sig, config())


def test_session_id_checks():
    assert valid_session_id("abc01")
    assert not valid_session_id("../../admin")
