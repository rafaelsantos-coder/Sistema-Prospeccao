import json
import pytest

from app.services.manual_meta_templates import eligible_templates, template_payload


def row(**overrides):
    return {
        "id": "123", "name": "oferta", "language": "pt_BR",
        "category": "MARKETING", "status": "APPROVED",
        "components": [{"type": "BODY", "text": "Olá, {{1}}! Plano {{2}}."},
                       {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Tenho interesse"}]}],
        **overrides,
    }


def test_only_approved_supported_meta_templates():
    rows = eligible_templates([
        row(),
        row(id="2", status="PENDING"),
        row(id="3", status="REJECTED"),
        row(id="4", components=[{"type": "BODY", "text": "Olá {{2}}"}]),
        row(id="5", components=[{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Olá"}]),
    ])
    assert [r["id"] for r in rows] == ["123"]
    assert rows[0]["variables"] == [1, 2]


def test_native_template_payload_and_personalization():
    selected = eligible_templates([row()])[0]
    template, body = template_payload(selected, json.dumps(["Ana", "650 Mega"]))
    assert body == "Olá, Ana! Plano 650 Mega."
    assert template["name"] == "oferta"
    assert template["language"] == {"code": "pt_BR"}
    assert template["components"][0]["parameters"] == [
        {"type": "text", "text": "Ana"}, {"type": "text", "text": "650 Mega"}
    ]
    assert template["components"][1]["sub_type"] == "quick_reply"


@pytest.mark.parametrize("raw", ["", "{}", '["Ana"]', '["Ana", ""]', '["Ana", 2]', '["Ana", "a\\nb"]'])
def test_parameters_fail_closed(raw):
    with pytest.raises(ValueError):
        template_payload(eligible_templates([row()])[0], raw)


def test_static_template_needs_no_parameters():
    selected = eligible_templates([row(components=[{"type": "BODY", "text": "Olá!"}])])[0]
    payload, body = template_payload(selected, "")
    assert payload == {"name": "oferta", "language": {"code": "pt_BR"}}
    assert body == "Olá!"
