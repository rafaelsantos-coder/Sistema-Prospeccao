from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIRun,
    AuditLog,
    Conversation,
    Message,
)
from app.services.ai_sdr_policy import CORPORATE_PLANS, FTTH_PLANS, MOBILE_PLANS
from app.services.audit import write_audit
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_supervisor")
REVIEW_ACTION = "AI_SUPERVISOR_SHADOW_REVIEW"
ERROR_ACTION = "AI_SUPERVISOR_SHADOW_ERROR"

SUPERVISOR_SYSTEM_PROMPT = """
Você é o SulIA Supervisor, auditor independente do SDR IA e do Vendedor IA da Sulnet.
Está em MODO SOMBRA: nunca altera mensagem, estado, CRM, follow-up ou fila. Apenas avalia
o que o agente fez e registra o que teria aprovado, corrigido ou bloqueado.

Avalie a ação usando somente evidências presentes no contexto. Mensagem INBOUND é fala do
cliente; OUTBOUND é fala da Sulnet. Não transforme fala da IA em fato do cliente.

Regras centrais:
1. SDR IA vende e qualifica. Não deve coletar CPF, data de nascimento, CEP de cadastro,
   e-mail contratual, dados bancários ou assinatura. Quando houver intenção clara de compra,
   deve entregar ao Vendedor IA. Pedido explícito por pessoa deve ir para humano.
2. Vendedor IA fecha a contratação. Não deve reiniciar prospecção, perguntar novamente
   informações já confirmadas ou ignorar o nextQuestion/missing do playbook do CRM.
3. Perguntas com opções fechadas devem usar quick replies/botões quando a plataforma suporta.
4. Não repetir a mesma pergunta, oferta ou CTA sem mudança de estratégia/contexto.
5. Não inventar plano, preço, cobertura, aprovação de crédito, prazo, contrato ou status CRM.
6. No funil sdrIaFunnel=true, o Vendedor IA não pergunta vencimento, taxa de ativação,
   unificação, metragem/NAP, anexos, ICCID, M2M ou tipo de assinatura. Móvel oferece apenas
   Número novo ou Portabilidade. Defaults do CRM são fonte de verdade.
7. Contrato precisa estar explicitamente assinado no CRM antes de mark_won/GANHO.
8. O agente deve continuar exatamente do estado atual; não reabrir etapas já concluídas.
9. Resposta comercial deve ser curta, natural e orientada ao próximo avanço real.
10. BLOCK significa: em modo Guardião futuro, a ação deveria ser impedida antes de chegar ao
    cliente por risco objetivo ou quebra de processo. CORRECT significa que a ação pode seguir,
    mas deveria ser reescrita/reorientada. APPROVE significa que está coerente.

Se houver dúvida factual e o contexto não provar um erro, não invente problema. Prefira
APPROVE com risco LOW e registre incerteza no rationale.
""".strip()


class SupervisorChecks(BaseModel):
    repeated_question: bool = False
    missing_quick_replies: bool = False
    wrong_agent_role: bool = False
    wrong_state_progression: bool = False
    crm_playbook_violation: bool = False
    commercial_policy_violation: bool = False
    hallucination_risk: bool = False
    continuity_problem: bool = False


class SupervisorReview(BaseModel):
    verdict: Literal["APPROVE", "CORRECT", "BLOCK"] = "APPROVE"
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    quality_score: int = Field(default=100, ge=0, le=100)
    issue_codes: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(default="", max_length=1600)
    suggested_reply: str = Field(default="", max_length=1800)
    suggested_state: str = Field(default="", max_length=120)
    suggested_buttons: list[str] = Field(default_factory=list, max_length=3)
    checks: SupervisorChecks = Field(default_factory=SupervisorChecks)


def supervisor_operational() -> bool:
    return bool(settings.ai_supervisor_shadow_enabled and settings.openai_api_key)


def _safe_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _redact_text(value: str | None) -> str:
    text = str(value or "")
    text = re.sub(r"(?<!\d)\d{15,22}(?!\d)", "[DADO_MOVEL]", text)
    text = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "[DOCUMENTO]", text)
    text = re.sub(r"(?<!\d)\d{5}-?\d{3}(?!\d)", "[CEP]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+", r"\1[REDACTED]", text)
    return text[:5000]


def _redact_object(value: Any, *, key: str = "") -> Any:
    sensitive = {
        "cpf",
        "cnpj",
        "clientcpf",
        "clientphone",
        "phone",
        "email",
        "clientemail",
        "authorization",
        "token",
        "password",
        "secret",
        "integrationkey",
        "debito_conta",
        "debito_agencia",
        "debito_banco",
        "pdfbase64",
    }
    key_folded = re.sub(r"[^a-z0-9_]+", "", str(key or "").casefold())
    if key_folded in sensitive:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_object(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_object(item) for item in value[:40]]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _normalized_similarity(a: str | None, b: str | None) -> float:
    left = _fold(a)
    right = _fold(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _closed_choice_question(text: str) -> bool:
    folded = _fold(text)
    if "?" not in str(text or ""):
        return False
    patterns = (
        "sim ou nao",
        "internet ou telefonia",
        "internet ou telefonia movel",
        "casa ou empresa",
        "para casa ou para empresa",
        "boleto ou debito",
        "numero novo ou portabilidade",
        "portabilidade ou numero novo",
        "whatsapp, e-mail ou ambos",
        "whatsapp ou e-mail",
        "unificar",
        "quer contratar",
        "deseja contratar",
        "quer aproveitar e contratar",
    )
    return any(pattern in folded for pattern in patterns)


def _catalog_snapshot() -> dict[str, Any]:
    return {
        "residential": [
            {
                "code": row.get("code"),
                "speed": row.get("download_mbps"),
                "price": row.get("price_with_fidelity"),
                "cities": row.get("cities"),
            }
            for row in FTTH_PLANS
        ],
        "corporate": [
            {
                "code": row.get("code"),
                "speed": row.get("download_mbps"),
                "price": row.get("price"),
            }
            for row in CORPORATE_PLANS
        ],
        "mobile": [
            {
                "code": row.get("code"),
                "gb": row.get("gb"),
                "price": row.get("price"),
            }
            for row in MOBILE_PLANS
        ],
    }


def _agent_role_for_user(db: Session, user_id: int | None) -> str:
    if not user_id:
        return ""
    agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.user_id == user_id)
    )
    return str(agent.role_type or "") if agent else ""


def _actual_outbound_after_run(
    db: Session,
    run: AIRun,
) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.direction == "OUTBOUND",
            Message.created_at >= run.started_at,
        )
        .order_by(Message.id.asc())
        .limit(1)
    )


def _history_for_review(
    db: Session,
    run: AIRun,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == run.conversation_id,
                Message.direction.in_(["INBOUND", "OUTBOUND"]),
            )
            .order_by(Message.id.desc())
            .limit(limit)
        )
    )
    messages.reverse()
    return [
        {
            "id": item.id,
            "direction": item.direction,
            "type": item.message_type,
            "body": _redact_text(item.body),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in messages
    ]


def _closing_context(
    db: Session,
    conversation_id: int,
) -> dict[str, Any]:
    state = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == conversation_id
        )
    )
    if not state:
        return {}
    collected = _safe_json(state.collected_json)
    playbook = collected.get("crmPlaybook")
    if not isinstance(playbook, dict):
        playbook = {}
    return {
        "step": state.step,
        "plan_label": state.plan_label,
        "offer_price": state.offer_price,
        "credit_status": state.credit_status,
        "contract_status": state.contract_status,
        "has_opportunity": bool(state.opportunity_id),
        "has_sign_url": bool(state.sign_url),
        "sdr_ia_funnel": bool(
            collected.get("_sdr_ia_funnel")
            or playbook.get("sdrIaFunnel")
        ),
        "playbook": {
            "mode": collected.get("_playbook_mode"),
            "ready_for_win": collected.get("_playbook_ready_for_win"),
            "missing_codes": collected.get("_playbook_missing_codes") or [],
            "next_question": _redact_text(
                collected.get("_playbook_question")
                or playbook.get("nextQuestion")
            ),
            "unmapped_code": collected.get("_playbook_unmapped_code"),
        },
    }


def _deterministic_findings(
    *,
    role: str,
    run: AIRun,
    run_output: dict[str, Any],
    history: list[dict[str, Any]],
    actual_outbound: Message | None,
    closing: dict[str, Any],
    ai_state: AIConversationState | None,
    lead_stage: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    reply = str(run.reply_text or "")
    folded_reply = _fold(reply)

    prior_outbounds = [
        str(item.get("body") or "")
        for item in history
        if item.get("direction") == "OUTBOUND"
        and item.get("id") != (actual_outbound.id if actual_outbound else None)
    ]
    if reply and any(_normalized_similarity(reply, item) >= 0.88 for item in prior_outbounds[-5:]):
        findings.append(
            {
                "code": "POSSIBLE_REPEAT",
                "severity": "MEDIUM",
                "detail": "A resposta é semanticamente muito parecida com uma saída recente.",
            }
        )

    quick_replies = run_output.get("quick_replies")
    has_buttons = bool(
        (isinstance(quick_replies, list) and quick_replies)
        or (actual_outbound and actual_outbound.message_type == "BUTTONS")
    )
    if _closed_choice_question(reply) and not has_buttons:
        findings.append(
            {
                "code": "MISSING_QUICK_REPLIES",
                "severity": "HIGH",
                "detail": "Pergunta de escolha fechada foi enviada sem botões/quick replies.",
            }
        )

    if role == "SDR":
        close_data_cues = (
            "qual seu cpf",
            "me informe seu cpf",
            "data de nascimento",
            "qual seu cep",
            "me envie seu cep",
            "e-mail para o contrato",
            "dados bancarios",
            "agencia",
            "conta bancaria",
        )
        if any(cue in folded_reply for cue in close_data_cues):
            findings.append(
                {
                    "code": "SDR_COLLECTING_CLOSING_DATA",
                    "severity": "HIGH",
                    "detail": "SDR aparenta coletar dado que pertence ao Vendedor IA.",
                }
            )

    if role == "CLOSER":
        prospecting_cues = (
            "ja e cliente sulnet",
            "internet ou telefonia movel",
            "para casa ou para empresa",
            "qual provedor voce usa",
            "quanto paga hoje",
        )
        if any(cue in folded_reply for cue in prospecting_cues):
            findings.append(
                {
                    "code": "CLOSER_RESTARTED_PROSPECTING",
                    "severity": "HIGH",
                    "detail": "Vendedor IA aparenta reabrir uma etapa de prospecção.",
                }
            )

        forbidden_steps = {
            "MOBILE_NEW_ICCID",
            "MOBILE_PORT_ICCID",
            "MOBILE_PORT_DATE",
            "MOBILE_M2M_QTY",
            "MOBILE_M2M_ICCIDS",
            "MOBILE_DUE_DAY",
            "MOBILE_ACTIVATION_FEE",
            "MOBILE_ACTIVATION_VALUE",
            "MOBILE_UNIFY",
            "MOBILE_UNIFY_REASON",
            "BILLING_DUE_DAY",
            "BILLING_ACTIVATION_FEE",
            "BILLING_ACTIVATION_VALUE",
            "BILLING_UNIFY",
            "BILLING_UNIFY_REASON",
        }
        step_after = str(run_output.get("step_after") or closing.get("step") or "")
        if closing.get("sdr_ia_funnel") and step_after in forbidden_steps:
            findings.append(
                {
                    "code": "SDR_IA_FORBIDDEN_CLOSING_STEP",
                    "severity": "CRITICAL",
                    "detail": f"Funil SDR IA entrou em etapa proibida: {step_after}.",
                }
            )

    signed_statuses = {
        "assinado",
        "signed",
        "contrato_assinado",
        "assinatura_concluida",
        "signature_completed",
    }
    contract_status = _fold(str(closing.get("contract_status") or "")).replace(" ", "_")
    if lead_stage == "GANHO" and closing and contract_status not in signed_statuses:
        findings.append(
            {
                "code": "WON_BEFORE_SIGNATURE",
                "severity": "CRITICAL",
                "detail": "Lead está GANHO sem confirmação explícita de assinatura no estado do closer.",
            }
        )

    if (
        ai_state
        and ai_state.state == "CLOSING"
        and role == "SDR"
        and str(run_output.get("handoff_target") or "") != "CLOSER_AI"
    ):
        findings.append(
            {
                "code": "SDR_REPLIED_AFTER_CLOSING",
                "severity": "HIGH",
                "detail": "Run do SDR ocorreu enquanto a conversa já estava em fechamento.",
            }
        )

    return findings


def build_supervisor_context(
    db: Session,
    run: AIRun,
) -> dict[str, Any]:
    agent = db.get(AIAgentProfile, run.agent_id)
    conversation = db.get(Conversation, run.conversation_id)
    ai_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == run.conversation_id
        )
    )
    run_output = _safe_json(run.output_json)
    run_input = _safe_json(run.input_snapshot_json)
    history = _history_for_review(db, run)
    actual_outbound = _actual_outbound_after_run(db, run)
    closing = _closing_context(db, run.conversation_id)
    role = str(agent.role_type if agent else "").upper() or "UNKNOWN"

    lead_stage = ""
    if conversation and conversation.lead:
        lead_stage = str(conversation.lead.stage or "")

    input_summary = {
        "latest_customer_message": _redact_text(
            run_input.get("latest_customer_message")
            or run_input.get("message")
        ),
        "lead": _redact_object(run_input.get("lead") or {}),
        "previous_ai_state": _redact_object(
            run_input.get("previous_ai_state") or {}
        ),
        "queue": _redact_object(run_input.get("queue") or {}),
        "step": run_input.get("step"),
    }

    findings = _deterministic_findings(
        role=role,
        run=run,
        run_output=run_output,
        history=history,
        actual_outbound=actual_outbound,
        closing=closing,
        ai_state=ai_state,
        lead_stage=lead_stage,
    )

    return {
        "mode": "SHADOW_ONLY_DO_NOT_INTERVENE",
        "run": {
            "id": run.id,
            "conversation_id": run.conversation_id,
            "agent_role": role,
            "agent_name": agent.name if agent else "",
            "model": run.model,
            "intent": run.intent,
            "reply_text": _redact_text(run.reply_text),
            "summary": _redact_text(run.summary),
            "output": _redact_object(run_output),
            "input_summary": input_summary,
        },
        "actual_outbound": {
            "id": actual_outbound.id if actual_outbound else None,
            "type": actual_outbound.message_type if actual_outbound else "",
            "body": _redact_text(actual_outbound.body) if actual_outbound else "",
            "sender_role": (
                _agent_role_for_user(db, actual_outbound.sender_user_id)
                if actual_outbound
                else ""
            ),
        },
        "history": history,
        "current_state": {
            "ai_state": ai_state.state if ai_state else "",
            "lead_stage": lead_stage,
            "closing": closing,
        },
        "deterministic_findings": findings,
        "approved_catalog": _catalog_snapshot(),
    }


def _critical_finding_codes(context: dict[str, Any]) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in context.get("deterministic_findings") or []
        if str(item.get("severity") or "") == "CRITICAL"
    }


def _high_finding_codes(context: dict[str, Any]) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in context.get("deterministic_findings") or []
        if str(item.get("severity") or "") in {"HIGH", "CRITICAL"}
    }


def _apply_shadow_guardrails(
    review: SupervisorReview,
    context: dict[str, Any],
) -> SupervisorReview:
    deterministic_codes = {
        str(item.get("code") or "")
        for item in context.get("deterministic_findings") or []
    }
    merged_codes = list(
        dict.fromkeys(
            [code for code in review.issue_codes if code]
            + [code for code in deterministic_codes if code]
        )
    )[:12]
    review.issue_codes = merged_codes

    critical = _critical_finding_codes(context)
    high = _high_finding_codes(context)
    if critical:
        review.verdict = "BLOCK"
        review.risk = "CRITICAL"
        review.quality_score = min(review.quality_score, 35)
    elif high and review.verdict == "APPROVE":
        review.verdict = "CORRECT"
        if review.risk == "LOW":
            review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 65)

    checks = review.checks
    checks.repeated_question = checks.repeated_question or "POSSIBLE_REPEAT" in deterministic_codes
    checks.missing_quick_replies = (
        checks.missing_quick_replies
        or "MISSING_QUICK_REPLIES" in deterministic_codes
    )
    checks.wrong_agent_role = (
        checks.wrong_agent_role
        or "SDR_COLLECTING_CLOSING_DATA" in deterministic_codes
        or "CLOSER_RESTARTED_PROSPECTING" in deterministic_codes
        or "SDR_REPLIED_AFTER_CLOSING" in deterministic_codes
    )
    checks.crm_playbook_violation = (
        checks.crm_playbook_violation
        or "SDR_IA_FORBIDDEN_CLOSING_STEP" in deterministic_codes
        or "WON_BEFORE_SIGNATURE" in deterministic_codes
    )
    return review


def run_supervisor_review(context: dict[str, Any]) -> SupervisorReview:
    if not supervisor_operational():
        raise RuntimeError("Supervisor IA sombra não está operacional.")

    from agents import Agent, Runner

    prompt = (
        "Audite esta ação concluída pelo SDR IA ou Vendedor IA. "
        "Você está em modo sombra e não pode alterar nada. "
        "Compare histórico, estado, playbook, ação realizada e achados determinísticos. "
        "Retorne uma avaliação estruturada.\n\n"
        + json.dumps(context, ensure_ascii=False, default=str)
    )
    supervisor = Agent(
        name="SulIA Supervisor Shadow",
        instructions=SUPERVISOR_SYSTEM_PROMPT,
        model=settings.ai_supervisor_model,
        model_settings={
            "reasoning": {
                "effort": settings.ai_supervisor_reasoning_effort or "low"
            },
            "verbosity": "low",
            "timeout": 45.0,
        },
        output_type=SupervisorReview,
    )
    result = Runner.run_sync(supervisor, prompt)
    review = result.final_output
    if not isinstance(review, SupervisorReview):
        review = SupervisorReview.model_validate(review)
    return _apply_shadow_guardrails(review, context)


def _followup_source_context(
    db: Session,
    audit: AuditLog,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(audit.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        conversation_id = int(audit.entity_id or payload.get("conversation_id") or 0)
        message_id = int(payload.get("message_id") or 0)
    except (TypeError, ValueError):
        return None
    if not conversation_id or not message_id:
        return None

    message = db.get(Message, message_id)
    conversation = db.get(Conversation, conversation_id)
    if not message or not conversation:
        return None

    agent = db.scalar(
        select(AIAgentProfile).where(
            AIAgentProfile.user_id == message.sender_user_id
        )
    )
    role = str(agent.role_type if agent else "").upper() or (
        "CLOSER" if audit.action == "AI_CLOSER_FOLLOWUP_SENT" else "SDR"
    )
    history = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction.in_(["INBOUND", "OUTBOUND"]),
            )
            .order_by(Message.id.desc())
            .limit(24)
        )
    )
    history.reverse()
    history_rows = [
        {
            "id": item.id,
            "direction": item.direction,
            "type": item.message_type,
            "body": _redact_text(item.body),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in history
    ]

    ai_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    closing = _closing_context(db, conversation_id)
    lead_stage = str(conversation.lead.stage or "") if conversation.lead else ""

    pseudo_run = type(
        "ShadowFollowup",
        (),
        {
            "reply_text": message.body,
            "conversation_id": conversation_id,
        },
    )()
    findings = _deterministic_findings(
        role=role,
        run=pseudo_run,
        run_output={
            "followup_step": payload.get("step"),
            "quick_replies": (
                ["present"]
                if int(payload.get("quick_reply_count") or payload.get("buttons") or 0) > 0
                else []
            ),
        },
        history=history_rows,
        actual_outbound=message,
        closing=closing,
        ai_state=ai_state,
        lead_stage=lead_stage,
    )

    return {
        "mode": "SHADOW_ONLY_DO_NOT_INTERVENE",
        "source": {
            "type": "FOLLOWUP",
            "audit_id": audit.id,
            "action": audit.action,
            "conversation_id": conversation_id,
            "agent_role": role,
            "agent_name": agent.name if agent else "",
            "followup_step": payload.get("step"),
            "theme": _redact_text(payload.get("theme")),
            "reply_text": _redact_text(message.body),
        },
        "actual_outbound": {
            "id": message.id,
            "type": message.message_type,
            "body": _redact_text(message.body),
            "sender_role": role,
        },
        "history": history_rows,
        "current_state": {
            "ai_state": ai_state.state if ai_state else "",
            "lead_stage": lead_stage,
            "closing": closing,
        },
        "deterministic_findings": findings,
        "approved_catalog": _catalog_snapshot(),
    }


def _reviewed_followup_audit_ids(
    db: Session,
    audit_ids: list[int],
) -> set[int]:
    if not audit_ids:
        return set()
    rows = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == REVIEW_ACTION,
                AuditLog.entity_type == "followup_audit",
                AuditLog.entity_id.in_([str(item) for item in audit_ids]),
            )
        )
    )
    result: set[int] = set()
    for row in rows:
        try:
            result.add(int(row.entity_id or ""))
        except (TypeError, ValueError):
            continue
    return result


def next_followup_for_shadow_review(
    db: Session,
    *,
    lookback_minutes: int = 30,
) -> AuditLog | None:
    threshold = utcnow() - timedelta(minutes=max(5, lookback_minutes))
    candidates = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action.in_(
                    ["AI_SDR_FOLLOWUP_SENT", "AI_CLOSER_FOLLOWUP_SENT"]
                ),
                AuditLog.created_at >= threshold,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .limit(80)
        )
    )
    if not candidates:
        return None
    reviewed = _reviewed_followup_audit_ids(
        db,
        [item.id for item in candidates],
    )
    for item in candidates:
        if item.id not in reviewed:
            return item
    return None


def record_followup_shadow_review(
    db: Session,
    *,
    audit: AuditLog,
    review: SupervisorReview,
    context: dict[str, Any],
) -> None:
    source = context.get("source") or {}
    payload = {
        "source_type": "FOLLOWUP",
        "source_audit_id": audit.id,
        "conversation_id": source.get("conversation_id"),
        "agent_role": source.get("agent_role"),
        "verdict": review.verdict,
        "risk": review.risk,
        "confidence": review.confidence,
        "quality_score": review.quality_score,
        "issue_codes": review.issue_codes,
        "rationale": review.rationale,
        "suggested_reply": review.suggested_reply,
        "suggested_state": review.suggested_state,
        "suggested_buttons": review.suggested_buttons,
        "checks": review.checks.model_dump(),
        "deterministic_findings": context.get("deterministic_findings") or [],
        "shadow_only": True,
        "intervened": False,
    }
    write_audit(
        db,
        user=None,
        action=REVIEW_ACTION,
        entity_type="followup_audit",
        entity_id=audit.id,
        payload=payload,
    )


def process_next_followup_shadow_review(db: Session) -> bool:
    if not supervisor_operational():
        return False
    audit = next_followup_for_shadow_review(db)
    if not audit:
        return False
    context = _followup_source_context(db, audit)
    if not context:
        return False
    try:
        review = run_supervisor_review(context)
        record_followup_shadow_review(
            db,
            audit=audit,
            review=review,
            context=context,
        )
        db.commit()
        source = context.get("source") or {}
        LOG.info(
            "ai_supervisor_shadow_followup audit=%s conversation=%s role=%s verdict=%s risk=%s",
            audit.id,
            source.get("conversation_id"),
            source.get("agent_role"),
            review.verdict,
            review.risk,
        )
        return True
    except Exception as exc:
        db.rollback()
        write_audit(
            db,
            user=None,
            action=ERROR_ACTION,
            entity_type="followup_audit",
            entity_id=audit.id,
            payload={
                "source_type": "FOLLOWUP",
                "conversation_id": (context.get("source") or {}).get("conversation_id"),
                "error": _redact_text(str(exc))[:1200],
                "shadow_only": True,
            },
        )
        db.commit()
        LOG.exception("ai_supervisor_shadow_followup_failed audit=%s", audit.id)
        return False


def _reviewed_run_ids(
    db: Session,
    run_ids: list[int],
) -> set[int]:
    if not run_ids:
        return set()
    rows = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == REVIEW_ACTION,
                AuditLog.entity_type == "ai_run",
                AuditLog.entity_id.in_([str(item) for item in run_ids]),
            )
        )
    )
    result: set[int] = set()
    for row in rows:
        try:
            result.add(int(row.entity_id or ""))
        except (TypeError, ValueError):
            continue
    return result


def _recent_error_run_ids(
    db: Session,
    run_ids: list[int],
) -> set[int]:
    if not run_ids:
        return set()
    threshold = utcnow() - timedelta(minutes=5)
    rows = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == ERROR_ACTION,
                AuditLog.entity_type == "ai_run",
                AuditLog.entity_id.in_([str(item) for item in run_ids]),
                AuditLog.created_at >= threshold,
            )
        )
    )
    result: set[int] = set()
    for row in rows:
        try:
            result.add(int(row.entity_id or ""))
        except (TypeError, ValueError):
            continue
    return result


def next_run_for_shadow_review(
    db: Session,
    *,
    lookback_minutes: int = 30,
) -> AIRun | None:
    agent_ids = list(
        db.scalars(
            select(AIAgentProfile.id).where(
                AIAgentProfile.active.is_(True),
                AIAgentProfile.role_type.in_(["SDR", "CLOSER"]),
            )
        )
    )
    if not agent_ids:
        return None

    threshold = utcnow() - timedelta(minutes=max(5, lookback_minutes))
    candidates = list(
        db.scalars(
            select(AIRun)
            .where(
                AIRun.agent_id.in_(agent_ids),
                AIRun.status == "CONCLUIDO",
                AIRun.finished_at.is_not(None),
                AIRun.finished_at >= threshold,
            )
            .order_by(AIRun.finished_at.asc(), AIRun.id.asc())
            .limit(80)
        )
    )
    if not candidates:
        return None

    run_ids = [item.id for item in candidates]
    reviewed = _reviewed_run_ids(db, run_ids)
    recent_errors = _recent_error_run_ids(db, run_ids)
    for run in candidates:
        if run.id in reviewed or run.id in recent_errors:
            continue
        return run
    return None


def record_shadow_review(
    db: Session,
    *,
    run: AIRun,
    review: SupervisorReview,
    context: dict[str, Any],
) -> None:
    agent = db.get(AIAgentProfile, run.agent_id)
    findings = context.get("deterministic_findings") or []
    payload = {
        "ai_run_id": run.id,
        "conversation_id": run.conversation_id,
        "agent_id": run.agent_id,
        "agent_role": str(agent.role_type if agent else ""),
        "verdict": review.verdict,
        "risk": review.risk,
        "confidence": review.confidence,
        "quality_score": review.quality_score,
        "issue_codes": review.issue_codes,
        "rationale": review.rationale,
        "suggested_reply": review.suggested_reply,
        "suggested_state": review.suggested_state,
        "suggested_buttons": review.suggested_buttons,
        "checks": review.checks.model_dump(),
        "deterministic_findings": findings,
        "shadow_only": True,
        "intervened": False,
    }
    write_audit(
        db,
        user=None,
        action=REVIEW_ACTION,
        entity_type="ai_run",
        entity_id=run.id,
        payload=payload,
    )


def process_next_shadow_review(db: Session) -> bool:
    if not supervisor_operational():
        return False
    run = next_run_for_shadow_review(db)
    if not run:
        return False

    try:
        context = build_supervisor_context(db, run)
        review = run_supervisor_review(context)
        record_shadow_review(
            db,
            run=run,
            review=review,
            context=context,
        )
        db.commit()
        LOG.info(
            "ai_supervisor_shadow run=%s conversation=%s verdict=%s risk=%s score=%s issues=%s",
            run.id,
            run.conversation_id,
            review.verdict,
            review.risk,
            review.quality_score,
            ",".join(review.issue_codes) or "-",
        )
        return True
    except Exception as exc:
        db.rollback()
        write_audit(
            db,
            user=None,
            action=ERROR_ACTION,
            entity_type="ai_run",
            entity_id=run.id,
            payload={
                "ai_run_id": run.id,
                "conversation_id": run.conversation_id,
                "error": _redact_text(str(exc))[:1200],
                "shadow_only": True,
            },
        )
        db.commit()
        LOG.exception(
            "ai_supervisor_shadow_failed run=%s conversation=%s",
            run.id,
            run.conversation_id,
        )
        return False


def run_shadow_supervisor_loop(stop_event: threading.Event | None = None) -> None:
    poll = max(2, int(settings.ai_supervisor_poll_seconds or 4))
    LOG.info(
        "SulIA Supervisor sombra iniciado. enabled=%s model=%s poll=%ss",
        settings.ai_supervisor_shadow_enabled,
        settings.ai_supervisor_model,
        poll,
    )
    while not (stop_event and stop_event.is_set()):
        if not supervisor_operational():
            time.sleep(max(10, poll))
            continue

        processed_run = False
        processed_followup = False
        db = SessionLocal()
        try:
            processed_run = process_next_shadow_review(db)
            processed_followup = process_next_followup_shadow_review(db)
        except Exception:
            db.rollback()
            LOG.exception("Falha inesperada no loop do Supervisor IA sombra.")
        finally:
            db.close()

        if not processed_run and not processed_followup:
            time.sleep(poll)
