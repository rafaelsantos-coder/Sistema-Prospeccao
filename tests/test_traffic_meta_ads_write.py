import io
import json
import urllib.parse

import httpx
import pytest

from app.services.meta_ads import MetaAdsError, MetaAdsSettings
from app.services.meta_ads_write import AdSpec, CampaignPlan, create_paused_campaign

SETTINGS = MetaAdsSettings(token="secret-token", account_id="act_1156163656960382", app_id="1", version="v21.0")


def _plan(budget=10.0):
    return CampaignPlan(
        name="t", daily_budget_brl=budget, city_name="Santo Ângelo", region_name="Rio Grande do Sul",
        phone="555535118003", welcome_text="Olá", utm_campaign="c",
        ads=[AdSpec("A1", "image", "h", "t", "d", "a1"), AdSpec("V1", "video", "h", "t", "d", "v1")],
    )


def _handler(posts):
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return str(1000 + counter["n"])

    def handler(request):
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/promote_pages"):
                return httpx.Response(200, json={"data": [{"id": "279511642170691", "name": "Sulnet Telecom"}]})
            if path.endswith("/search"):
                return httpx.Response(200, json={"data": [{"key": "268655", "name": "Santo Ângelo", "region": "Rio Grande do Sul"}]})
            return httpx.Response(200, json={"status": {"video_status": "ready"}, "picture": "https://x/thumb.jpg"})
        body = request.content.decode("latin-1")
        form = {}
        if "multipart" not in request.headers.get("content-type", ""):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        posts.append((path, form))
        if path.endswith("/adimages"):
            return httpx.Response(200, json={"images": {"a.jpg": {"hash": "HASH1"}}})
        return httpx.Response(200, json={"id": next_id()})
    return handler


def test_everything_is_created_paused_with_budget_in_cents():
    posts = []
    client = httpx.Client(transport=httpx.MockTransport(_handler(posts)))
    out = create_paused_campaign(
        _plan(), image=("a.jpg", b"img"), video=("v.mp4", io.BytesIO(b"vid")), settings=SETTINGS, client=client,
    )
    assert out.campaign_id and out.adset_id and len(out.ad_ids) == 2
    for path, form in posts:
        if path.endswith(("/campaigns", "/adsets", "/ads")):
            assert form["status"] == "PAUSED"
    adset = next(f for p, f in posts if p.endswith("/adsets"))
    assert adset["daily_budget"] == "1000"
    assert json.loads(adset["promoted_object"])["whatsapp_phone_number"] == "555535118003"
    assert json.loads(adset["targeting"])["geo_locations"]["cities"][0]["key"] == "268655"


def test_budget_outside_safety_limit_is_refused_before_any_call():
    def handler(request):
        raise AssertionError("não deveria chamar a Meta")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MetaAdsError):
        create_paused_campaign(_plan(budget=5000), image=("a.jpg", b"i"), video=("v.mp4", io.BytesIO(b"v")), settings=SETTINGS, client=client)


def test_error_reports_what_was_created_and_never_leaks_token():
    def handler(request):
        if request.method == "GET":
            return _handler([])(request)
        if request.url.path.endswith("/adsets"):
            return httpx.Response(400, json={"error": {"code": 100, "error_subcode": 1, "message": "parâmetro inválido"}})
        return _handler([])(request)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MetaAdsError) as info:
        create_paused_campaign(_plan(), image=("a.jpg", b"i"), video=("v.mp4", io.BytesIO(b"v")), settings=SETTINGS, client=client)
    text = str(info.value)
    assert "Campanha criada (pausada)" in text and "secret-token" not in text
