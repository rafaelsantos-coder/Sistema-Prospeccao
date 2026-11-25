from unittest.mock import Mock

import pytest

from app.routers import meta_campaigns


def test_template_body_and_examples():
    payload = meta_campaigns.build_template(
        "oferta_fibra", "MARKETING", "Olá {{1}}, confira nossa oferta.",
        "Ana", "Sulnet", "Responda para saber mais", "Tenho interesse\nNão quero"
    )
    assert payload["language"] == "pt_BR"
    assert payload["components"][1]["example"] == {"body_text": [["Ana"]]}
    assert payload["components"][-1]["buttons"][0]["type"] == "QUICK_REPLY"


@pytest.mark.parametrize("body,examples", [("Olá {{2}}", "Ana"), ("Olá {{1}}", ""), ("Olá {{nome}}", "Ana")])
def test_invalid_variables(body, examples):
    with pytest.raises(ValueError):
        meta_campaigns.build_template("oferta", "MARKETING", body, examples)


def test_invalid_category():
    with pytest.raises(ValueError):
        meta_campaigns.build_template("oferta", "OTHER", "Olá")


def test_only_official_digital_accounts(monkeypatch):
    official = Mock(id=1)
    other = Mock(id=2)
    monkeypatch.setattr(meta_campaigns, "list_meta_accounts", lambda db: [official, other])
    monkeypatch.setattr(meta_campaigns, "is_official_digital", lambda a: a.id == 1)
    monkeypatch.setattr(meta_campaigns, "default_queue_for_meta_account", lambda db, account_id: Mock())
    monkeypatch.setattr(meta_campaigns, "is_digital_queue", lambda q: True)
    assert meta_campaigns._accounts(None) == [official]
