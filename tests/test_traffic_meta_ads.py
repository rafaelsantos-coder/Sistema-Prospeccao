import httpx

from app.services.meta_ads import MetaAdsError, MetaAdsSettings, check_connection, fetch_campaigns

SETTINGS = MetaAdsSettings(token="secret-token", account_id="act_1156163656960382", app_id="1", version="v21.0")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_handler(accounts):
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/me"):
            return httpx.Response(200, json={"id": "1", "name": "Gestor Trafego API"})
        if path.endswith("/me/permissions"):
            return httpx.Response(200, json={"data": [
                {"permission": "ads_management", "status": "granted"},
                {"permission": "ads_read", "status": "granted"},
            ]})
        if path.endswith("/promote_pages"):
            return httpx.Response(200, json={"data": [{"id": "279511642170691", "name": "Sulnet Telecom"}]})
        if path.endswith("/me/adaccounts"):
            return httpx.Response(200, json={"data": accounts})
        return httpx.Response(200, json={
            "name": "Sulnet 03 - Sistema Próprio", "account_status": 1,
            "currency": "BRL", "timezone_name": "America/Sao_Paulo",
        })
    return handler


def test_connection_ok_when_token_sees_only_test_account():
    report = check_connection(SETTINGS, client=_client(_ok_handler([{"id": "act_1156163656960382", "name": "x"}])))
    assert report.all_ok
    assert len(report.accessible_accounts) == 1


def test_connection_flags_extra_accounts_as_not_isolated():
    accounts = [{"id": "act_1156163656960382", "name": "x"}, {"id": "act_999999999", "name": "outra"}]
    report = check_connection(SETTINGS, client=_client(_ok_handler(accounts)))
    assert not report.all_ok
    assert [c.ok for c in report.checks][-1] is False


def test_missing_token_reports_variable_without_network():
    report = check_connection(MetaAdsSettings(token="", account_id="act_1156163656960382", app_id="", version="v21.0"))
    assert not report.all_ok
    assert "META_ADS_ACCESS_TOKEN" in report.checks[0].detail


def test_expired_token_message_never_contains_token():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": 190, "message": "Bearer secret-token expired"}})
    report = check_connection(SETTINGS, client=_client(handler))
    assert not report.all_ok
    assert all("secret-token" not in c.detail for c in report.checks)


def test_fetch_campaigns_computes_cost_per_conversation():
    def handler(request):
        assert request.url.path.endswith("/act_1156163656960382/campaigns")
        assert "last_7d" in request.url.params["fields"]
        return httpx.Response(200, json={"data": [
            {"id": "1", "name": "A", "effective_status": "PAUSED", "objective": "OUTCOME_ENGAGEMENT",
             "daily_budget": "5000", "insights": {"data": [{
                 "spend": "44.12", "impressions": "1000", "clicks": "50", "ctr": "5.0",
                 "actions": [{"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "2"}]}]}},
            {"id": "2", "name": "B", "effective_status": "PAUSED", "objective": "X"},
        ]})
    rows = fetch_campaigns("7d", SETTINGS, client=_client(handler))
    assert [r.id for r in rows] == ["1", "2"]
    assert rows[0].conversations == 2 and rows[0].daily_budget == 50.0
    assert round(rows[0].cost_per_conversation, 2) == 22.06
    assert rows[1].cost_per_conversation is None and rows[1].spend == 0


def test_fetch_campaigns_without_token_raises_safe_error():
    try:
        fetch_campaigns("7d", MetaAdsSettings(token="", account_id="act_1156163656960382", app_id="", version="v21.0"))
    except MetaAdsError as exc:
        assert "configurada" in str(exc)
    else:
        raise AssertionError("esperava MetaAdsError")
