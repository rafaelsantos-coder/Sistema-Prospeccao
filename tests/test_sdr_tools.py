"""Ferramentas do SDR e modo observação das regras fixas."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from app.services import ai_sdr
from app.services.ai_sdr import AIDecision, QualificationData
from app.services.ai_sdr_policy import catalog_for
from app.services.ai_sdr_tools import search_manual


def test_catalog_for_city_lists_only_plans_available_there():
    santo_angelo = catalog_for("santo angelo", "RESIDENCIAL")
    assert santo_angelo["city"] == "Santo Ângelo"
    speeds = [plan["speed"] for plan in santo_angelo["plans"]]
    assert speeds == [800, 650]
    assert santo_angelo["plans"][1]["price_with_fidelity"] == 99.90

    crissiumal = catalog_for("Crissiumal", "residencial")
    assert [plan["speed"] for plan in crissiumal["plans"]] == [650, 250]

    outside = catalog_for("Porto Alegre", "RESIDENCIAL")
    assert outside["available"] is False and outside["plans"] == []

    unknown = catalog_for("", "RESIDENCIAL")
    assert unknown["city"] is None and "pergunte a cidade" in unknown["note"].lower()


def test_catalog_for_corporate_and_mobile():
    assert [p["price"] for p in catalog_for("", "Empresa")["plans"]] == [139.00, 169.00, 199.90]
    assert catalog_for("", "MOVEL")["plans"][0]["gb"] == 5


def test_search_manual_ranks_relevant_items():
    manual = [
        {"title": "Condições contratuais FTTH", "tags": "fidelidade,multa", "content": "Fidelidade de 12 meses."},
        {"title": "Roteadores inclusos", "tags": "roteador", "content": "1 roteador incluso; adicional R$ 29,90."},
    ]
    found = search_manual(manual, "quantos roteadores vem no plano?")
    assert found[0]["title"] == "Roteadores inclusos"
    assert search_manual(manual, "???") == []


def _decision(reply: str = "Temos 800 Mega por R$ 119,90.") -> AIDecision:
    return AIDecision(
        reply_text=reply,
        intent="INTEREST",
        qualification_score=50,
        temperature="MORNO",
        recommended_stage="QUALIFICADO",
        handoff_recommended=False,
        confidence=0.9,
        summary="",
        next_action="",
        qualification=QualificationData(),
    )


def test_observed_policy_is_measured_but_does_not_rewrite(monkeypatch):
    def rewrite(decision, context):
        decision.reply_text = "Texto da regra fixa"
        return decision

    monkeypatch.setattr(ai_sdr, "SDR_REWRITE_POLICIES", (("router_information", rewrite),))
    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, ai_sdr_observe_policies="router_information"))
    decision, observations = ai_sdr._apply_rewrite_policies(_decision(), {})
    assert decision.reply_text == "Temos 800 Mega por R$ 119,90."
    assert observations == [{"policy": "router_information", "mode": "observe", "changed": ["reply_text"]}]

    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, ai_sdr_observe_policies=""))
    decision, observations = ai_sdr._apply_rewrite_policies(_decision(), {})
    assert decision.reply_text == "Texto da regra fixa"
    assert observations[0]["mode"] == "rewrite"


def test_policy_observations_survive_copy_and_are_not_in_model_schema():
    decision = _decision()
    decision._policy_observations = [{"policy": "x"}]
    assert decision.model_copy(deep=True)._policy_observations == [{"policy": "x"}]
    assert "_policy_observations" not in json.dumps(AIDecision.model_json_schema())


def test_sdr_agent_receives_catalog_and_manual_tools(monkeypatch):
    from app.services import ai_runtime

    captured = {}

    def fake_run(agent, prompt):
        captured["tools"] = [tool.name for tool in agent.tools]
        captured["prompt"] = prompt
        return SimpleNamespace(final_output=_decision())

    monkeypatch.setattr(ai_runtime, "run_agent_sync", fake_run)
    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, openai_api_key="sk-test", ai_sdr_tools_enabled=True))
    agent = SimpleNamespace(
        id=0, name="SulIA", system_prompt="", model="m", reasoning_effort="low", execution_mode="SHADOW",
    )
    monkeypatch.setattr("app.services.ai_sdr_tools.object_session", lambda obj: None)
    monkeypatch.setattr(ai_sdr, "_effective_execution_mode", lambda agent: "SHADOW")

    decision = ai_sdr.run_ai_decision(agent, {"history": [], "latest_customer_message": "oi"})

    assert captured["tools"] == ["consultar_catalogo", "buscar_manual"]
    assert "consultar_catalogo" in captured["prompt"]
    assert isinstance(decision, AIDecision)
