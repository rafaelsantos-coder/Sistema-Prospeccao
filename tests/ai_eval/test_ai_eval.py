"""Avaliação do SDR com o modelo de IA de verdade.

Roda só quando AI_EVAL=1 e OPENAI_API_KEY estão definidos (workflow
"Avaliação da IA", manual ou semanal). Cada caso monta o contexto como o
worker monta, chama run_ai_decision com o prompt e o manual atuais e confere o
comportamento esperado. Use antes de mudar prompt, manual, modelo ou regras.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

CASES = sorted((Path(__file__).parent / "cases").glob("*.json"))

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AI_EVAL") == "1" and os.environ.get("OPENAI_API_KEY")),
    reason="Avaliação com modelo: defina AI_EVAL=1 e OPENAI_API_KEY.",
)


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+", " ", text)


def _context(case: dict) -> dict:
    from app.services.ai_sdr import _confirmed_facts_from_history
    from app.services.ai_sdr_policy import SYSTEM_KNOWLEDGE_ITEMS

    history = [
        {"direction": row["direction"], "type": "TEXT", "body": row["body"], "created_at": None}
        for row in case["history"]
    ]
    latest = case["latest"]
    full = history + [{"direction": "INBOUND", "type": "TEXT", "body": latest, "created_at": None}]
    lead = case.get("lead") or {}
    return {
        "agent": {"name": "SulIA SDR Digital", "role": "SDR", "execution_mode": "AUTO"},
        "queue": {"id": 0, "name": "Avaliação", "team": "DIGITAL"},
        "confirmed_facts": _confirmed_facts_from_history(full),
        "learning_experiment": {},
        "lead": {
            "id": None,
            "contact_name": "",
            "city": lead.get("city", ""),
            "stage": "CONTATO_REALIZADO",
            "score": 0,
            "temperature": "MORNO",
            "confirmed_customer_type": "",
            "confirmed_existing_customer": "",
            "journey": {},
        },
        "previous_ai_state": {"qualification_score": 0, "temperature": "MORNO", "last_intent": "UNKNOWN", "state": "AI_ACTIVE"},
        "customer_evidence": [
            {"body": row["body"], "created_at": None} for row in full if row["direction"] == "INBOUND"
        ],
        "history": full,
        "latest_customer_message": latest,
        "knowledge": [
            {"title": item["title"], "category": item["category"], "content": item["content"]}
            for item in SYSTEM_KNOWLEDGE_ITEMS
        ],
    }


@pytest.mark.parametrize("path", CASES, ids=[path.stem for path in CASES])
def test_sdr_behaviour(path: Path):
    from app.services import ai_sdr
    from app.services.ai_sdr_policy import SULIA_SYSTEM_PROMPT

    case = json.loads(path.read_text(encoding="utf-8"))
    agent = SimpleNamespace(
        id=0,
        name="SulIA SDR Digital",
        role_type="SDR",
        system_prompt=SULIA_SYSTEM_PROMPT,
        model=os.environ.get("AI_EVAL_MODEL", "gpt-6-luna"),
        reasoning_effort="low",
        execution_mode="AUTO",
    )
    decision = ai_sdr.run_ai_decision(agent, _context(case))
    reply = _fold(decision.reply_text)
    expect = case["expect"]

    if expect.get("reply_contains_any"):
        assert any(_fold(item) in reply for item in expect["reply_contains_any"]), decision.reply_text
    for forbidden in expect.get("reply_not_contains", []):
        assert _fold(forbidden) not in reply, decision.reply_text
    if expect.get("handoff_target_in"):
        assert decision.handoff_target in expect["handoff_target_in"], (
            decision.handoff_target,
            decision.reply_text,
        )
