from app.services.instagram_prospecting import classify_comment, preview_comments


def change(cid="100", text="Qual o valor?", author="201", media="301"):
    return {"field": "comments", "value": {
        "id": cid, "text": text, "from": {"id": author, "username": "private-user"},
        "media": {"id": media, "media_product_type": "FEED"},
    }}


def test_commercial_rules_and_priority():
    assert classify_comment("Qual o valor?") == "price"
    assert classify_comment("Tem cobertura em Santa Rosa?") == "coverage"
    assert classify_comment("Tenho interesse!") == "interest"
    assert classify_comment("Parabéns!") == "praise"
    assert classify_comment("Sem internet, quero cancelar o plano!") == "support_review"
    assert classify_comment("Não tenho interesse nesse plano.") == "opt_out"
    assert classify_comment("Olá, pessoal") == "manual_review"
    assert classify_comment("Olá, pessoal", "all") == "generic"
    assert classify_comment("@amigo", "all") == "manual_review"
    assert classify_comment("💙", "all") == "manual_review"


def test_preview_dedup_filter_self_and_privacy():
    payloads = [
        change(), change(), change("101", "Sem internet"),
        change("102", "Quero", author="999"),
        change("103", "Quero", media="302"),
        {"field": "mentions"}, {"field": "comments", "value": {}},
    ]
    result = preview_comments(payloads, "999", media_id="301")
    assert result["counts"] == {
        "duplicate": 1, "filtered": 1, "ignored": 1, "invalid": 1,
        "price": 1, "self": 1, "support_review": 1,
    }
    assert result["simulation_only"] is True
    assert result["send_enabled"] is False
    assert set(result["draft_templates"]) == {"price"}
    assert "private-user" not in repr(result)
    assert "Sem internet" not in repr(result)


def test_live_not_automated():
    event = change()
    event["value"]["media"]["media_product_type"] = "LIVE"
    assert preview_comments([event], "999", "all")["counts"] == {"manual_review": 1}
