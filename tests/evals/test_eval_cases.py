"""Casos de teste vindos de conversas reais perdidas (diagnóstico SulIA).

Cada arquivo em tests/evals/cases/ descreve uma situação que já deu errado em
produção e o comportamento esperado. Este verificador roda todos a cada PR; o
merge na main exige este check verde, então nenhuma mudança de regra, prompt
ou código vai para produção se piorar um caso antigo.

Para criar um caso novo a partir de uma conversa ruim: IA > Qualidade das
conversas > "Gerar caso" baixa o histórico anonimizado; transforme-o num dos
tipos abaixo (ou em "conversation_replay", que fica documentado até existir
avaliação com o modelo no CI) e adicione o arquivo aqui.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import ai_followups, ai_sdr, ai_seller
from app.services.ai_sdr_policy import catalog_for
from app.timeutils import UTC


CASES_DIR = Path(__file__).parent / "cases"
CASES = sorted(CASES_DIR.glob("*.json"))
SP = ZoneInfo("America/Sao_Paulo")
REQUIRED_KEYS = {"id", "conversation", "description", "kind", "input", "expect"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seller_state(data: dict) -> SimpleNamespace:
    base = {
        "plan_label": "",
        "offer_price": None,
        "step": "BILLING",
        "handoff_summary": "",
        "opportunity_id": 1,
        "contract_status": "",
        "sign_url": "",
        "agent_id": 1,
    }
    base.update(data)
    return SimpleNamespace(**base)


def _utc(local: str) -> datetime:
    return datetime.fromisoformat(local).replace(tzinfo=SP).astimezone(UTC).replace(tzinfo=None)


def _local(value: datetime) -> str:
    return value.replace(tzinfo=UTC).astimezone(SP).strftime("%Y-%m-%d %H:%M")


def run_case(case: dict) -> None:
    kind = case["kind"]
    data = case["input"]
    expect = case["expect"]

    if kind == "sdr_fast_purchase":
        found = ai_sdr._fast_purchase_request(data)
        if expect.get("plan_speed") is None:
            assert found is None
            return
        assert found is not None, "intenção de compra não reconhecida"
        assert found["plan"]["download_mbps"] == expect["plan_speed"]
        assert found["city"] == expect["city"]

    elif kind == "sdr_existing_customer":
        value = ai_sdr._existing_customer_from_text(
            data["message"], previous_outbound=data["previous_outbound"]
        )
        assert value == expect["value"]

    elif kind == "sdr_plan_change":
        assert ai_sdr._existing_customer_plan_change_requested(data) is expect["value"]

    elif kind == "catalog":
        result = catalog_for(data["city"], data["segment"])
        assert [plan["speed"] for plan in result["plans"]] == expect["speeds"]
        prices = {str(plan["speed"]): plan["price_with_fidelity"] for plan in result["plans"]}
        for speed, price in expect["price_with_fidelity"].items():
            assert prices[speed] == price

    elif kind == "seller_contract_knowledge":
        state = _seller_state(data["state"])
        collected = dict(data["collected"])
        for message, fragments in zip(data["messages"], expect["replies_contain"], strict=True):
            reply = ai_seller._seller_contract_knowledge_reply(state, collected, message) or ""
            for fragment in fragments:
                assert fragment.lower() in reply.lower(), f"{message!r} → {reply!r}"

    elif kind == "seller_billing":
        collected: dict = {}
        for answer in data["answers"]:
            collected.update(ai_seller._billing_merge(collected, answer))
        for key, value in expect.items():
            assert collected.get(key) == value

    elif kind == "seller_contract_gate":
        class _NoContract:
            def __getattr__(self, name):
                raise AssertionError(f"contrato acionado sem aceite: {name}")

        state = _seller_state(data["state"])
        collected = dict(data["collected"])
        reply = ai_seller._finish_sale_contract(_NoContract(), state, collected)
        assert state.step == expect["step"]
        for fragment in expect["contains"]:
            assert fragment in reply

    elif kind == "seller_acceptance":
        for answer, expected in data["answers"].items():
            assert ai_seller._sale_acceptance_answer(answer) == expected, answer

    elif kind == "seller_question":
        for message, expected in data["messages"].items():
            assert ai_seller._seller_is_question(message) is expected, message

    elif kind == "followup_window":
        for local, expected in data["local_times"].items():
            assert _local(ai_followups.business_window_start(_utc(local))) == expected, local

    elif kind == "conversation_replay":
        pytest.skip("Caso documentado; requer avaliação com o modelo de IA.")

    else:
        raise AssertionError(f"Tipo de caso desconhecido: {kind}")


def test_cases_exist_and_are_well_formed():
    assert CASES, "nenhum caso de teste encontrado"
    ids = set()
    for path in CASES:
        case = _load(path)
        assert REQUIRED_KEYS <= case.keys(), path.name
        assert case["id"] not in ids, f"id repetido: {case['id']}"
        ids.add(case["id"])


@pytest.mark.parametrize("path", CASES, ids=[path.stem for path in CASES])
def test_eval_case(path: Path):
    run_case(_load(path))
