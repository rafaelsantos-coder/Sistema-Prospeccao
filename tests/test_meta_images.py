import json

import httpx
import pytest

from app.services import meta_images
from app.services.meta_whatsapp import MetaWhatsAppService, meta_inbound_to_engine_payload


def test_old_meta_image_callback_preserves_reference():
    payload = meta_inbound_to_engine_payload(
        {"type": "image", "image": {"id": "123456", "mime_type": "image/jpeg"}},
        contact_wa_id="5555999999999",
    )
    assert meta_images.image_ref(json.dumps(payload)) == "123456"
    assert meta_images.image_ref('{"image":{"meta_media_id":"https://evil.invalid"}}') is None
    assert meta_images.image_ref("not json") is None


def test_meta_download_uses_token_only_on_trusted_hosts(monkeypatch):
    original = httpx.Client
    calls = []
    data = b"\xff\xd8\xff" + b"test"
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.host == "graph.facebook.com":
            assert request.url.params["phone_number_id"] == "42"
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/attachment", "mime_type": "image/jpeg"})
        assert request.url.host == "lookaside.fbsbx.com"
        return httpx.Response(200, content=data)
    monkeypatch.setattr(meta_images.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    service = MetaWhatsAppService(phone_number_id="42", access_token="test-token")
    assert meta_images.download_meta_image(service, "123", 1024) == (data, "image/jpeg", "imagem.jpg")
    assert len(calls) == 2


@pytest.mark.parametrize("url", ["https://evil.invalid/a", "http://lookaside.fbsbx.com/a", "https://lookaside.fbsbx.com.evil.invalid/a"])
def test_meta_download_blocks_untrusted_hosts(monkeypatch, url):
    original = httpx.Client
    def handler(request):
        assert request.url.host == "graph.facebook.com"
        return httpx.Response(200, json={"url": url, "mime_type": "image/jpeg"})
    monkeypatch.setattr(meta_images.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    service = MetaWhatsAppService(phone_number_id="42", access_token="test-token")
    with pytest.raises(ValueError):
        meta_images.download_meta_image(service, "123", 1024)
