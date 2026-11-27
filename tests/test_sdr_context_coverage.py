"""Regressão: resposta humana com município fora da cobertura, antes da Black."""
import pytest

from app.services.ai_sdr_policies import (
    _confirmed_city_from_text,
    _confirmed_facts_from_history,
    confirmed_out_of_area_city,
    no_fiber_coverage_decision,
)


def _row(direction, body):
    return {"direction": direction, "body": body}


@pytest.mark.parametrize("answer", [
    "São Pedro das Missões", "SÃO PEDRO DAS MISSÕES - RS", "Sou de São Pedro das Missões",
])
def test_city_outside_catalog_is_not_discarded(answer):
    assert _confirmed_city_from_text(answer, "O senhor é de qual cidade?") == "São Pedro das Missões"


@pytest.mark.parametrize("answer", ["Sim", "Não", "Não sei", "Quero internet", "Boa tarde"])
def test_non_city_answer_is_not_stored_as_city(answer):
    assert not _confirmed_city_from_text(answer, "O senhor é de qual cidade?")


def test_historical_outside_city_stops_black_and_handoff():
    history = [
        _row("OUTBOUND", "O senhor é de qual cidade?"),
        _row("INBOUND", "São Pedro das Missões"),
        _row("OUTBOUND", "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"),
        _row("INBOUND", "Não"),
    ]
    facts = _confirmed_facts_from_history(history)
    context = {"history": history, "confirmed_facts": facts, "latest_customer_message": "Não"}
    assert facts["city"] == "São Pedro das Missões"
    decision = no_fiber_coverage_decision(context)
    assert decision is not None
    assert "São Pedro das Missões" in decision.reply_text
    assert "cobertura de fibra" in decision.reply_text
    assert "Black" not in decision.reply_text
    assert decision.offer_plan_code is None
    assert decision.handoff_target == "NONE"
    assert not decision.followup_needed
    assert not decision.customer_commitment


def test_customer_can_correct_city_or_request_mobile():
    history = [
        _row("OUTBOUND", "O senhor é de qual cidade?"),
        _row("INBOUND", "São Pedro das Missões"),
    ]
    assert confirmed_out_of_area_city({"history": history}) == "São Pedro das Missões"
    assert no_fiber_coverage_decision({
        "history": history + [_row("INBOUND", "A instalação será em Santa Rosa")],
        "latest_customer_message": "A instalação será em Santa Rosa",
    }) is None
    assert no_fiber_coverage_decision({
        "history": history + [_row("INBOUND", "Quero um plano móvel")],
        "latest_customer_message": "Quero um plano móvel",
    }) is None


def test_city_with_similar_name_does_not_match_served_city():
    history = [
        _row("OUTBOUND", "O senhor é de qual cidade?"),
        _row("INBOUND", "Santa Rosa de Lima"),
    ]
    assert confirmed_out_of_area_city({"history": history}) == "Santa Rosa de Lima"
