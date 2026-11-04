"""Motor genérico de políticas fixas do SDR.

As regras comerciais (catálogo, cidade, Black etc.) continuam definidas no
módulo do SDR. Este arquivo só aplica a lista, mede o efeito e respeita o
modo observação (`AI_SDR_OBSERVE_POLICIES`) sem novo deploy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

LOG = logging.getLogger("uvicorn.error")


class RewriteDecision(Protocol):
    reply_text: str
    quick_replies: list[Any]
    intent: str
    sales_state: str
    handoff_target: str
    handoff_recommended: bool
    offer_plan_code: str | None
    journey_state: str

    def model_copy(self, *, deep: bool = False) -> RewriteDecision: ...


PolicyFn = Callable[[Any, dict[str, Any]], Any]
PolicySpec = tuple[str, PolicyFn]


def decision_signature(decision: RewriteDecision) -> dict[str, Any]:
    return {
        "reply_text": decision.reply_text,
        "quick_replies": [item.label for item in decision.quick_replies],
        "intent": decision.intent,
        "sales_state": decision.sales_state,
        "handoff_target": decision.handoff_target,
        "handoff_recommended": decision.handoff_recommended,
        "offer_plan_code": decision.offer_plan_code,
        "journey_state": decision.journey_state,
    }


def observed_policy_names(raw: str | None) -> set[str]:
    return {
        item.strip()
        for item in str(raw or "").split(",")
        if item.strip()
    }


def apply_rewrite_policies(
    decision: Any,
    context: dict[str, Any],
    policies: Sequence[PolicySpec],
    *,
    observe: set[str] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Aplica regras fixas que reescrevem a resposta, medindo cada uma.

    Nomes em ``observe`` só registram o que mudariam; os demais reescrevem de
    verdade. O registro alimenta o AIRun e o analista semanal.
    """
    observed = observe or set()
    observations: list[dict[str, Any]] = []
    for name, policy in policies:
        before = decision_signature(decision)
        candidate = policy(decision.model_copy(deep=True), context)
        after = decision_signature(candidate)
        changed = [key for key in before if before[key] != after[key]]
        if not changed:
            continue
        mode = "observe" if name in observed else "rewrite"
        observations.append({"policy": name, "mode": mode, "changed": changed})
        if mode == "rewrite":
            decision = candidate
    if observations:
        LOG.info("sdr_policy_effects %s", json.dumps(observations, ensure_ascii=False))
    return decision, observations
