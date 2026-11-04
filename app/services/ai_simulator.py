"""Simulador do SDR: conversa de teste com a SulIA sem WhatsApp.

Roda a mesma sequência do worker (caminho rápido de compra, decisão do modelo
com ferramentas, regras fixas, integridade da jornada e Guardião), com o
agente, o prompt e a base de conhecimento de produção, mas não envia nada pela
Z-API e não grava lead, conversa ou estado. Quem chama deve dar rollback na
sessão depois de cada turno.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIAgentProfile
from app.services.ai_quick_replies import quick_replies_for_message


def _sdr_agent(db: Session) -> AIAgentProfile:
    agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital")
    ) or db.scalar(select(AIAgentProfile).where(AIAgentProfile.role_type == "SDR").limit(1))
    if not agent:
        raise RuntimeError("Agente SDR não encontrado.")
    return agent


def build_simulated_context(
    db: Session,
    agent: AIAgentProfile,
    history: list[dict[str, str]],
    message: str,
    *,
    city: str = "",
) -> dict[str, Any]:
    from app.services.ai_sdr import _confirmed_facts_from_history, retrieve_knowledge

    rows = [
        {
            "direction": "OUTBOUND" if row.get("direction") == "OUTBOUND" else "INBOUND",
            "type": "TEXT",
            "body": str(row.get("body") or "")[:2000],
            "created_at": None,
        }
        for row in history[-40:]
    ]
    rows.append({"direction": "INBOUND", "type": "TEXT", "body": message[:2000], "created_at": None})
    knowledge = retrieve_knowledge(db, agent=agent, query=message)
    return {
        "agent": {
            "name": agent.name,
            "role": agent.role_type,
            "execution_mode": "AUTO",
            "business_goal": agent.business_goal,
            "handoff_score": agent.qualification_handoff_score,
        },
        "queue": {"id": 0, "name": "Simulador", "team": "DIGITAL"},
        "confirmed_facts": _confirmed_facts_from_history(rows),
        "learning_experiment": {},
        "lead": {
            "id": None,
            "contact_name": "",
            "city": city,
            "stage": "CONTATO_REALIZADO",
            "score": 0,
            "temperature": "MORNO",
            "confirmed_customer_type": "",
            "confirmed_existing_customer": "",
            "journey": {},
        },
        "previous_ai_state": {
            "qualification_score": 0,
            "temperature": "MORNO",
            "last_intent": "UNKNOWN",
            "state": "AI_ACTIVE" if history else "NEW",
        },
        "customer_evidence": [
            {"body": row["body"], "created_at": None} for row in rows if row["direction"] == "INBOUND"
        ],
        "history": rows,
        "latest_customer_message": message,
        "knowledge": [
            {"title": item.title, "category": item.category, "content": item.content}
            for item in knowledge
        ],
    }


def _guard(db: Session, agent: AIAgentProfile, context: dict, decision) -> dict[str, Any]:
    from app.services.ai_guardian import (
        GuardianReview,
        _apply_guardrails,
        _normalize_buttons,
        _run_guardian_model,
        guardian_operational,
    )
    from app.services.ai_supervisor import _catalog_snapshot, _deterministic_findings, _redact_text

    buttons = _normalize_buttons(
        decision.reply_text, [item.model_dump() for item in decision.quick_replies]
    )
    history = [
        {"direction": row["direction"], "type": "TEXT", "body": _redact_text(row["body"]), "created_at": None}
        for row in context["history"][-24:]
    ]
    findings = _deterministic_findings(
        role="SDR",
        run=SimpleNamespace(reply_text=decision.reply_text, conversation_id=0),
        run_output={
            "quick_replies": buttons,
            "step_after": decision.journey_state,
            "journey_state": decision.journey_state,
            "proposed_state": decision.journey_state,
        },
        history=history,
        actual_outbound=None,
        closing={},
        ai_state=None,
        lead_stage="",
    )
    guardian_context = {
        "mode": "GUARDIAN_PRE_SEND",
        "source": "SDR_SIMULATOR",
        "conversation_id": 0,
        "agent": {"id": agent.id, "role": "SDR", "name": agent.name},
        "proposed_action": {
            "reply_text": _redact_text(decision.reply_text),
            "buttons": buttons,
            "proposed_state": decision.journey_state or decision.sales_state,
            "metadata": {
                "intent": decision.intent,
                "handoff_target": decision.handoff_target,
                "customer_commitment": decision.customer_commitment,
                "tool_facts": list((getattr(decision, "_timing", {}) or {}).get("tool_facts") or []),
            },
        },
        "history": history,
        "current_state": {"ai_state": "AI_ACTIVE", "lead_stage": "", "closing": {}},
        "deterministic_findings": findings,
        "approved_catalog": _catalog_snapshot(),
    }
    from app.services.ai_guardian import guardian_enforcing, guardian_shadowing

    if (guardian_enforcing() or guardian_shadowing()) and guardian_operational():
        review = _run_guardian_model(guardian_context)
    else:
        review = GuardianReview(
            verdict="APPROVE", risk="LOW", confidence=1.0, quality_score=100,
            rationale="Guardião desativado.",
        )
    result = _apply_guardrails(
        db,
        review=review,
        context=guardian_context,
        original_text=decision.reply_text,
        original_buttons=buttons,
    )
    if guardian_shadowing() and not guardian_enforcing():
        return {
            "verdict": "APPROVE",
            "observed_verdict": result.verdict,
            "shadow_only": True,
            "should_send": True,
            "issue_codes": list(result.issue_codes),
            "rationale": f"[sombra] observou {result.verdict}: {result.rationale}",
            "reply_text": decision.reply_text,
            "buttons": [item.get("label") for item in buttons],
        }
    return {
        "verdict": result.verdict,
        "should_send": result.should_send,
        "issue_codes": list(result.issue_codes),
        "rationale": result.rationale,
        "reply_text": result.reply_text,
        "buttons": [item.get("label") for item in (result.buttons or [])],
    }


SIMULATOR_EFFORTS = {"none", "minimal", "low", "medium"}


def _variant_agent(agent: AIAgentProfile, variant: dict[str, Any] | None):
    """Cópia do agente com o nível de raciocínio e/ou o prompt enxuto do teste.

    Nada é gravado: a produção continua com a configuração do banco.
    """
    variant = variant or {}
    effort = str(variant.get("reasoning_effort") or "").strip().lower()
    compact = bool(variant.get("compact_prompt"))
    if effort not in SIMULATOR_EFFORTS and not compact:
        return agent, "produção"
    from app.services.ai_sdr_policy import SULIA_COMPACT_SYSTEM_PROMPT

    copy = SimpleNamespace(
        id=agent.id,
        name=agent.name,
        role_type=agent.role_type,
        model=agent.model,
        execution_mode=agent.execution_mode,
        business_goal=agent.business_goal,
        qualification_handoff_score=agent.qualification_handoff_score,
        user=getattr(agent, "user", None),
        user_id=getattr(agent, "user_id", None),
        reasoning_effort=effort if effort in SIMULATOR_EFFORTS else agent.reasoning_effort,
        system_prompt=SULIA_COMPACT_SYSTEM_PROMPT if compact else agent.system_prompt,
    )
    label = f"raciocínio {copy.reasoning_effort}" + (" · prompt enxuto" if compact else "")
    return copy, label


def simulate_sdr_turn(
    db: Session,
    *,
    history: list[dict[str, str]],
    message: str,
    city: str = "",
    variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services import ai_sdr

    started = time.monotonic()
    production_agent = _sdr_agent(db)
    agent, variant_label = _variant_agent(production_agent, variant)
    context = build_simulated_context(db, production_agent, history, message, city=city)
    trace: list[str] = [f"Configuração: {variant_label}."]

    fast = ai_sdr._fast_purchase_request(context)
    if fast:
        plan, fast_city = fast["plan"], fast["city"]
        decision = ai_sdr._fast_purchase_decision(plan, fast_city, handoff=bool(fast_city))
        trace.append(
            "Caminho rápido de compra: "
            + ("transferido ao Vendedor IA." if fast_city else "pergunta a cidade antes de transferir.")
        )
        return _result(decision, trace, started, guardian=None, integrity=[])

    decision = ai_sdr.run_ai_decision(agent, context)
    trace.append(_model_trace(decision))
    integrity = ai_sdr._journey_integrity_issues(decision, context)
    if integrity:
        trace.append("Integridade da jornada barrou: " + ", ".join(integrity) + ". Regenerando.")
        retry_context = dict(context)
        retry_context["integrity_guard"] = {
            "issues": integrity,
            "instruction": (
                "A primeira decisão violou a integridade da jornada. Preserve confirmed_facts, "
                "não volte a perguntas já respondidas e avance para a próxima informação pendente."
            ),
        }
        retry_context["anti_repetition"] = {
            "forbidden_previous_messages": ai_sdr._recent_outbound_messages_for_repetition(context),
            "reason": "A primeira decisão repetiu ou regrediu uma etapa já concluída.",
        }
        decision = ai_sdr.run_ai_decision(agent, retry_context)
        trace.append(_model_trace(decision, retry=True))
        integrity = ai_sdr._journey_integrity_issues(decision, context)

    if ai_sdr._is_closer_handoff(decision):
        trace.append("Transferido ao Vendedor IA (sem revisão: a mensagem do SDR não é enviada).")
        return _result(decision, trace, started, guardian=None, integrity=integrity)
    guardian_started = time.monotonic()
    guardian = _guard(db, agent, context, decision)
    trace.append(f"Guardião: {(time.monotonic() - guardian_started):.1f} s.")
    if not guardian["should_send"]:
        trace.append(
            "Guardião bloqueou (" + (", ".join(guardian["issue_codes"]) or guardian["rationale"]) + "). Regenerando com o motivo."
        )
        retry_context = dict(context)
        retry_context["guardian_feedback"] = {
            "issue_codes": guardian["issue_codes"],
            "rationale": guardian["rationale"],
            "blocked_reply": decision.reply_text,
            "instruction": (
                "Gere outra resposta corrigindo os problemas apontados pelo Guardião. "
                "Não repita a mensagem bloqueada e não altere fatos comerciais sem evidência."
            ),
        }
        decision = ai_sdr.run_ai_decision(agent, retry_context)
        trace.append(_model_trace(decision, retry=True))
        guardian = _guard(db, agent, context, decision)
        if not guardian["should_send"]:
            trace.append("Guardião bloqueou de novo: em produção o cliente iria para um consultor.")
            decision.reply_text = ai_sdr.HUMAN_FALLBACK_NOTICE
            decision.quick_replies = []
            decision.handoff_target = "HUMAN"
    if guardian["should_send"] and guardian["verdict"] in {"REWRITE", "CORRECT_STATE"} and guardian["reply_text"]:
        trace.append("Guardião reescreveu a resposta.")
        decision.reply_text = guardian["reply_text"]
        from app.services.ai_quick_replies import QuickReply

        decision.quick_replies = [
            QuickReply(id=f"g{index}", label=label[:20])
            for index, label in enumerate(guardian["buttons"], start=1)
            if label
        ]
    return _result(decision, trace, started, guardian=guardian, integrity=integrity)


def _model_trace(decision, *, retry: bool = False) -> str:
    timing = getattr(decision, "_timing", {}) or {}
    tools = ", ".join(timing.get("tool_calls") or []) or "nenhuma"
    seconds = (timing.get("model_ms") or 0) / 1000
    label = "Nova geração do modelo" if retry else "Modelo"
    return f"{label}: {seconds:.1f} s (ferramentas: {tools})."


def _result(decision, trace, started, *, guardian, integrity) -> dict[str, Any]:
    buttons = quick_replies_for_message(decision.reply_text, decision.quick_replies)
    return {
        "reply": decision.reply_text,
        "buttons": [getattr(item, "label", None) or item.get("label") for item in buttons],
        "intent": decision.intent,
        "sales_state": decision.sales_state,
        "journey_state": decision.journey_state,
        "handoff_target": decision.handoff_target,
        "objection": decision.objection,
        "offer_plan_code": decision.offer_plan_code,
        "offer_price": decision.offer_price,
        "free_months": decision.free_months,
        "policy_observations": list(getattr(decision, "_policy_observations", []) or []),
        "integrity_issues": integrity,
        "guardian": guardian,
        "trace": trace,
        "timing": dict(getattr(decision, "_timing", {}) or {}),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
