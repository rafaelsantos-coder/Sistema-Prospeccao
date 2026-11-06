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
    assert catalog_for("", "MOVEL")["plans"][0]["gb"] == 2


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

    decision = ai_sdr.run_ai_decision(
        agent, {"history": [], "latest_customer_message": "Qual a multa se eu cancelar?"}
    )

    assert captured["tools"] == ["consultar_catalogo", "buscar_manual", "calcular_multa"]

    decision = ai_sdr.run_ai_decision(
        agent,
        {
            "history": [],
            "latest_customer_message": "Quero avançar",
            "confirmed_facts": {"city": "Santa Rosa", "customer_type": "RESIDENCIAL"},
        },
    )
    assert captured["tools"] == []
    assert "consultar_catalogo" in captured["prompt"]
    assert isinstance(decision, AIDecision)


def test_ai_recovery_answers_and_keeps_the_conversation_with_ai(monkeypatch):
    # 06/11: "Bom dia" sem resposta e "vou chamar um consultor". Agora a IA
    # mesma responde e a conversa continua AI_ACTIVE.
    calls = []
    monkeypatch.setattr(ai_sdr, "_apply_crm_decision", lambda db, **kw: calls.append(("crm", kw["decision"].handoff_target)))
    monkeypatch.setattr(
        ai_sdr,
        "_send_autonomous_reply",
        lambda db, **kw: calls.append(("send", kw["decision"].reply_text)) or SimpleNamespace(id=77),
    )
    monkeypatch.setattr(ai_sdr, "write_audit", lambda db, **kw: calls.append(("audit", kw["action"])))
    monkeypatch.setattr(
        "app.services.ai_followups.schedule_followup", lambda db, **kw: calls.append(("followup", kw["step"]))
    )

    class _Db:
        added = []

        def scalar(self, _stmt):
            return None

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            return None

    db = _Db()
    job = SimpleNamespace(id=1, conversation_id=10, queue_id=4, message_id=9, status="PROCESSANDO", error=None, attempts=1)
    run = SimpleNamespace(id=5, status="PROCESSANDO", error=None, reply_text="", output_json="", finished_at=None)
    agent = SimpleNamespace(id=3, user=None)
    decision = _decision()
    decision.handoff_target = "HUMAN"

    outcome = ai_sdr._send_ai_recovery(
        db,
        agent=agent,
        job=job,
        run=run,
        decision=decision,
        context={"latest_customer_message": "Bom dia", "history": [], "confirmed_facts": {}, "lead": {}},
        reason="Guardião bloqueou: X",
    )

    assert outcome == "ai_recovery"
    sent = [item[1] for item in calls if item[0] == "send"][0]
    assert sent.endswith("você já é cliente da Sulnet?")
    assert ("crm", "NONE") in calls
    assert ("audit", "AI_SDR_RECOVERY") in calls
    assert ("followup", 1) in calls
    assert job.status == "CONCLUIDO"
    assert db.added[0].state == "AI_ACTIVE"
    assert not ai_sdr._reply_promises_human(sent)


def test_recovery_continues_where_the_conversation_stopped():
    context = {
        "latest_customer_message": "bom dia",
        "history": [{"direction": "OUTBOUND", "body": "Temos 650 Mega por R$ 99,90."}],
        "confirmed_facts": {"city": "Santa Rosa", "customer_type": "RESIDENCIAL"},
        "lead": {},
    }
    text, buttons = ai_sdr._ai_recovery_reply(context)
    assert text.startswith("Bom dia! Vamos continuar de onde paramos.")
    assert "Santa Rosa" in text and buttons


def test_reply_promising_a_human_is_replaced():
    for text in (
        "Quero te responder com precisão, então vou chamar um consultor da Sulnet para continuar com você.",
        "Perfeito! Vou encaminhar seu atendimento para um vendedor concluir a contratação com você.",
        "Um atendente vai continuar com você por aqui.",
    ):
        assert ai_sdr._reply_promises_human(text), text
    for text in (
        "Temos 800 Mega por R$ 119,90. Quer contratar?",
        "O técnico vai até sua casa em até 45 dias úteis.",
    ):
        assert not ai_sdr._reply_promises_human(text), text
    decision = _decision("Vou chamar um consultor para te ajudar.")
    decision.handoff_target = "HUMAN"
    result = ai_sdr._never_hand_to_human(decision, {"latest_customer_message": "Oi", "history": [], "lead": {}})
    assert result.handoff_target == "NONE"
    assert not ai_sdr._reply_promises_human(result.reply_text)


def test_penalty_tool_matches_seller_rule():
    from app.services.ai_sdr_tools import ftth_cancellation_penalty

    result = ftth_cancellation_penalty(650, 1, 6)
    assert result["penalty"] == 479.95
    assert "R$ 479,95" in result["answer"]
    assert ftth_cancellation_penalty(800, 0, 12)["penalty"] == 0
    assert ftth_cancellation_penalty(999, 0, 1)["ok"] is False


def test_official_catalog_goes_into_context_when_city_is_known():
    with_city = ai_sdr._official_catalog_for_context(
        {"confirmed_facts": {"city": "Santo Ângelo", "customer_type": "RESIDENCIAL"}, "lead": {}}
    )
    assert [plan["speed"] for plan in with_city["plans"]] == [800, 650]
    company = ai_sdr._official_catalog_for_context({"confirmed_facts": {"customer_type": "EMPRESA"}})
    assert company["segment"] == "EMPRESA"
    assert ai_sdr._official_catalog_for_context({"confirmed_facts": {}, "lead": {}}) is None


def _high_intent(reply="Perfeito! Vamos seguir."):
    decision = _decision(reply)
    decision.intent = "HIGH_INTENT"
    decision.journey_state = "CONFIRM_PURCHASE"
    return decision


def test_clear_purchase_after_offer_goes_to_closing():
    context = {
        "latest_customer_message": "Quero avançar",
        "history": [
            {"direction": "OUTBOUND", "body": "Temos 650 Mega por R$ 99,90 mensais. O que acha?"},
            {"direction": "INBOUND", "body": "Quero avançar"},
        ],
    }
    decision = ai_sdr._promote_clear_purchase_to_closing(_high_intent(), context)
    assert decision.handoff_target == "CLOSER_AI"
    assert ai_sdr._is_closer_handoff(decision)


def test_purchase_promotion_needs_offer_and_no_hesitation():
    no_offer = {"latest_customer_message": "Quero avançar", "history": []}
    assert ai_sdr._promote_clear_purchase_to_closing(_high_intent(), no_offer).handoff_target != "CLOSER_AI"
    hesitant = {
        "latest_customer_message": "Quero avançar, mas achei caro",
        "history": [{"direction": "OUTBOUND", "body": "Temos 800 Mega por R$ 119,90."}],
    }
    assert ai_sdr._promote_clear_purchase_to_closing(_high_intent(), hesitant).handoff_target != "CLOSER_AI"
    question = {
        "latest_customer_message": "Quero avançar, como funciona a instalação?",
        "history": [{"direction": "OUTBOUND", "body": "Temos 800 Mega por R$ 119,90."}],
    }
    assert ai_sdr._promote_clear_purchase_to_closing(_high_intent(), question).handoff_target != "CLOSER_AI"


def test_tool_results_are_kept_as_evidence_for_the_guardian(monkeypatch):
    from app.services import ai_runtime

    def fake_run(agent, prompt):
        return SimpleNamespace(
            final_output=_decision("A multa fica em R$ 479,95."),
            new_items=[
                SimpleNamespace(type="tool_call_item", raw_item=SimpleNamespace(name="calcular_multa")),
                SimpleNamespace(type="tool_call_output_item", output='{"penalty": 479.95}'),
            ],
        )

    monkeypatch.setattr(ai_runtime, "run_agent_sync", fake_run)
    monkeypatch.setattr(ai_sdr, "settings", replace(ai_sdr.settings, openai_api_key="sk-test"))
    monkeypatch.setattr("app.services.ai_sdr_tools.object_session", lambda obj: None)
    monkeypatch.setattr(ai_sdr, "_effective_execution_mode", lambda agent: "SHADOW")
    agent = SimpleNamespace(id=0, name="SulIA", system_prompt="", model="m", reasoning_effort="low", execution_mode="SHADOW")

    decision = ai_sdr.run_ai_decision(
        agent, {"history": [], "latest_customer_message": "Quanto é a multa no 6º mês?"}
    )

    assert decision._timing["tool_calls"] == ["calcular_multa"]
    assert decision._timing["tool_facts"] == [{"tool": "calcular_multa", "result": '{"penalty": 479.95}'}]


def _two_no_context(latest="Não"):
    return {
        "latest_customer_message": latest,
        "history": [
            {"direction": "OUTBOUND", "body": "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"},
            {"direction": "INBOUND", "body": "Não"},
            {"direction": "OUTBOUND", "body": "Quer conhecer nossa promoção Black da Sulnet?"},
            {"direction": "INBOUND", "body": latest},
        ],
    }


def test_two_bare_no_answers_close_the_conversation():
    # Conversa de 06/11 (Loine): "Não" duas vezes e a IA seguiu perguntando.
    from app.services import ai_sdr_policies

    decision = _decision("Qual caminho você quer seguir primeiro: Internet ou Telefonia Móvel?")
    result = ai_sdr_policies._apply_repeated_rejection_policy(decision, _two_no_context())
    assert result.reply_text == ai_sdr_policies.REJECTION_CLOSE_MESSAGE
    assert result.intent == "NOT_INTERESTED"
    assert result.followup_needed is False


def test_single_no_does_not_close_yet():
    from app.services import ai_sdr_policies

    context = {
        "latest_customer_message": "Não",
        "history": [
            {"direction": "OUTBOUND", "body": "Quer conhecer nossa promoção Black da Sulnet?"},
            {"direction": "INBOUND", "body": "Não"},
        ],
    }
    decision = _decision("Tudo bem! Quer ver a Telefonia Móvel?")
    result = ai_sdr_policies._apply_repeated_rejection_policy(decision, context)
    assert result.reply_text == "Tudo bem! Quer ver a Telefonia Móvel?"


def test_relato_de_problema_na_internet_vai_para_a_central_sem_venda():
    # Conversa Rafael Trennepohl (06/11): "Estou com problema na internet" e a IA ofereceu plano.
    from app.services import ai_sdr_policies

    context = {"latest_customer_message": "Estou com problema na internet", "history": []}
    decision = _decision("Para sua próxima contratação, prefere Telefonia Móvel?")
    result = ai_sdr_policies._apply_existing_customer_plan_change_policy(decision, context)
    assert result.reply_text == ai_sdr_policies.CENTRAL_REDIRECT_MESSAGE
    assert result.offer_plan_code is None
