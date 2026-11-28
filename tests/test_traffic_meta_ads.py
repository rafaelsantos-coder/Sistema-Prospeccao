import httpx

from app.services.meta_ads import MetaAdsSettings, check_connection

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
