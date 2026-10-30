from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    Conversation,
    Message,
)
from app.services.ai_quick_replies import quick_replies_for_message
from app.services.ai_supervisor import (
    _catalog_snapshot,
    _closing_context,
    _deterministic_findings,
    _redact_object,
    _redact_text,
)
from app.services.audit import write_audit


LOG = logging.getLogger("sulnet_ai_guardian")
GUARDIAN_ACTION = "AI_SUPERVISOR_GUARDIAN_REVIEW"
GUARDIAN_ERROR_ACTION = "AI_SUPERVISOR_GUARDIAN_ERROR"


class GuardianBlockedError(RuntimeError):
    def __init__(self, result: "GuardianResult"):
        super().__init__(
            "Guardião bloqueou a ação: "
            + (", ".join(result.issue_codes) or result.rationale or result.verdict)
        )
        self.result = result


GUARDIAN_SYSTEM_PROMPT = """
Você é o SulIA Guardião, a camada de controle em tempo real do SDR IA e do Vendedor IA
da Sulnet. Você avalia uma ação ANTES de ela chegar ao cliente.

Seu veredito deve ser um destes:
- APPROVE: a mensagem e o estado estão coerentes e podem seguir.
- REWRITE: a mensagem pode seguir somente depois de ser reescrita sem alterar fatos comerciais.
- BLOCK: a ação não pode chegar ao cliente; existe quebra objetiva de processo ou risco alto.
- CORRECT_STATE: existe um erro objetivo de estado que precisa ser corrigido antes de seguir.

Regras obrigatórias:
1. Nunca invente plano, preço, cobertura, aprovação de crédito, prazo, contrato, assinatura ou
   status do CRM. Catálogo e playbook presentes no contexto são fonte de verdade.
2. SDR não coleta CPF, data de nascimento, CEP contratual, e-mail contratual, dados bancários
   ou assinatura. Intenção clara de compra deve seguir para o Vendedor IA.
3. Vendedor IA continua exatamente do ponto atual do fechamento e não reabre prospecção.
4. Perguntas com 2 ou 3 respostas previsíveis devem usar botões/quick replies.
5. Não repita pergunta, oferta ou CTA recente sem mudança real de estratégia.
6. No funil sdrIaFunnel=true, não use etapas proibidas de ICCID, M2M, vencimento, taxa,
   unificação, metragem/NAP, anexos ou tipo de assinatura. Defaults/playbook do CRM prevalecem.
7. Nunca permita GANHO antes de assinatura confirmada pelo CRM.
8. Em REWRITE preserve todos os fatos comerciais válidos da mensagem original. Não acrescente
   valores, velocidades, benefícios ou condições que não estejam comprovados no contexto.
9. CORRECT_STATE só deve ser usado quando o contexto provar o estado correto. Não adivinhe.
10. Se houver dúvida factual, não crie um problema novo. Prefira APPROVE quando não houver
    evidência objetiva de erro.

Para REWRITE, preencha reply_text com a mensagem final pronta para envio.
Para perguntas fechadas, preencha suggested_buttons com 2 ou 3 rótulos curtos.
Para CORRECT_STATE, preencha corrected_state e, quando fizer sentido, reply_text.
""".strip()


class GuardianReview(BaseModel):
    verdict: Literal["APPROVE", "REWRITE", "BLOCK", "CORRECT_STATE"] = "APPROVE"
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    quality_score: int = Field(default=100, ge=0, le=100)
    issue_codes: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(default="", max_length=1600)
    reply_text: str = Field(default="", max_length=1800)
    suggested_buttons: list[str] = Field(default_factory=list, max_length=3)
    corrected_state: str = Field(default="", max_length=120)


class GuardianResult(BaseModel):
    verdict: Literal["APPROVE", "REWRITE", "BLOCK", "CORRECT_STATE"] = "APPROVE"
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    quality_score: int = Field(default=100, ge=0, le=100)
    issue_codes: list[str] = Field(default_factory=list)
    rationale: str = ""
    reply_text: str = ""
    buttons: list[dict[str, str]] = Field(default_factory=list)
    corrected_state: str = ""
    state_corrected: bool = False
    should_send: bool = True


def guardian_operational() -> bool:
    return bool(
        settings.ai_supervisor_guardian_enabled
        and settings.openai_api_key
    )


def _recent_history(
    db: Session,
    conversation_id: int,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction.in_(["INBOUND", "OUTBOUND"]),
            )
            .order_by(Message.id.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return [
        {
            "id": item.id,
            "direction": item.direction,
            "type": item.message_type,
            "body": _redact_text(item.body),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in rows
    ]


def _role(agent: AIAgentProfile | None) -> str:
    return str(agent.role_type if agent else "").upper() or "UNKNOWN"


def _normalize_buttons(
    text: str,
    buttons: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    return quick_replies_for_message(text, buttons or [])


def _buttons_from_labels(labels: list[str]) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(labels[:3], start=1):
        label = str(raw or "").strip()[:20]
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        clean.append({"id": f"guardian_{index}", "label": label})
    return clean


def build_guardian_context(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    reply_text: str,
    buttons: list[dict[str, str]] | None = None,
    source: str,
    proposed_state: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    role = _role(agent)
    history = _recent_history(db, conversation.id)
    ai_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    closing = _closing_context(db, conversation.id)
    lead_stage = (
        str(conversation.lead.stage or "")
        if conversation.lead
        else ""
    )
    normalized_buttons = _normalize_buttons(reply_text, buttons)

    pseudo_run = SimpleNamespace(
        reply_text=reply_text,
        conversation_id=conversation.id,
    )
    findings = _deterministic_findings(
        role=role,
        run=pseudo_run,
        run_output={
            "quick_replies": normalized_buttons,
            "step_after": proposed_state or closing.get("step"),
        },
        history=history,
        actual_outbound=None,
        closing=closing,
        ai_state=ai_state,
        lead_stage=lead_stage,
    )

    return {
        "mode": "GUARDIAN_PRE_SEND",
        "source": source,
        "conversation_id": conversation.id,
        "agent": {
            "id": agent.id,
            "role": role,
            "name": agent.name,
        },
        "proposed_action": {
            "reply_text": _redact_text(reply_text),
            "buttons": _redact_object(normalized_buttons),
            "proposed_state": proposed_state,
            "metadata": _redact_object(metadata or {}),
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


def _run_guardian_model(context: dict[str, Any]) -> GuardianReview:
    from agents import Agent, Runner

    prompt = (
        "Valide esta ação antes do envio. Use os achados determinísticos como evidência, "
        "mas também confira histórico, continuidade, estado, catálogo e playbook. "
        "Retorne a ação final estruturada.\n\n"
        + json.dumps(context, ensure_ascii=False, default=str)
    )
    guardian = Agent(
        name="SulIA Guardian",
        instructions=GUARDIAN_SYSTEM_PROMPT,
        model=settings.ai_supervisor_model,
        model_settings={
            "reasoning": {
                "effort": settings.ai_supervisor_reasoning_effort or "low"
            },
            "verbosity": "low",
            "timeout": float(settings.ai_supervisor_guardian_timeout_seconds or 30),
        },
        output_type=GuardianReview,
    )
    result = Runner.run_sync(guardian, prompt)
    review = result.final_output
    return review if isinstance(review, GuardianReview) else GuardianReview.model_validate(review)


def _severity_sets(context: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    critical: set[str] = set()
    high: set[str] = set()
    all_codes: set[str] = set()
    for item in context.get("deterministic_findings") or []:
        code = str(item.get("code") or "")
        severity = str(item.get("severity") or "").upper()
        if not code:
            continue
        all_codes.add(code)
        if severity == "CRITICAL":
            critical.add(code)
            high.add(code)
        elif severity == "HIGH":
            high.add(code)
    return critical, high, all_codes


def _safe_state_correction(
    db: Session,
    *,
    conversation_id: int,
    role: str,
    review: GuardianReview,
    context: dict[str, Any],
) -> bool:
    if review.verdict != "CORRECT_STATE" or role != "CLOSER":
        return False

    _critical, _high, codes = _severity_sets(context)
    if "SDR_IA_FORBIDDEN_CLOSING_STEP" not in codes:
        return False
    if review.corrected_state != "WAITING_CRM_READY":
        review.corrected_state = "WAITING_CRM_READY"

    state = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == conversation_id
        )
    )
    if not state:
        return False

    state.step = "WAITING_CRM_READY"
    state.last_error = ""
    return True


def _apply_guardrails(
    db: Session,
    *,
    review: GuardianReview,
    context: dict[str, Any],
    original_text: str,
    original_buttons: list[dict[str, str]],
) -> GuardianResult:
    critical, high, deterministic_codes = _severity_sets(context)
    review.issue_codes = list(
        dict.fromkeys(
            [str(item) for item in review.issue_codes if str(item)]
            + sorted(deterministic_codes)
        )
    )[:12]

    # Invariantes que o modelo não pode afrouxar.
    if "WON_BEFORE_SIGNATURE" in critical:
        review.verdict = "BLOCK"
        review.risk = "CRITICAL"
        review.quality_score = min(review.quality_score, 20)
        review.reply_text = ""

    elif "SDR_IA_FORBIDDEN_CLOSING_STEP" in critical:
        review.verdict = "CORRECT_STATE"
        review.risk = "CRITICAL"
        review.quality_score = min(review.quality_score, 35)
        review.corrected_state = "WAITING_CRM_READY"
        if not review.reply_text.strip():
            review.reply_text = (
                "Vou seguir pela etapa correta da sua contratação e validar os dados "
                "que você já informou."
            )

    elif "SDR_REPLIED_AFTER_CLOSING" in high:
        review.verdict = "BLOCK"
        review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 40)
        review.reply_text = ""

    elif "SDR_COLLECTING_CLOSING_DATA" in high:
        # Reescrever a frase não corrige a responsabilidade do agente. Bloqueia para
        # o SDR regenerar a decisão antes de qualquer atualização de CRM/estado.
        review.verdict = "BLOCK"
        review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 45)
        review.reply_text = ""

    elif "CLOSER_RESTARTED_PROSPECTING" in high:
        if review.reply_text.strip():
            review.verdict = "REWRITE"
        else:
            review.verdict = "BLOCK"
        review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 55)

    # Pergunta fechada sem botões: corrige antes do envio.
    if "MISSING_QUICK_REPLIES" in high and review.verdict != "BLOCK":
        labels = _buttons_from_labels(review.suggested_buttons)
        candidate_text = review.reply_text.strip() or original_text
        candidate_buttons = labels or _normalize_buttons(candidate_text, [])
        if len(candidate_buttons) >= 2:
            review.verdict = "REWRITE"
            review.reply_text = candidate_text
            review.risk = "HIGH"
            review.quality_score = min(review.quality_score, 65)
        else:
            review.verdict = "BLOCK"
            review.risk = "HIGH"
            review.quality_score = min(review.quality_score, 45)

    if "POSSIBLE_REPEAT" in deterministic_codes and review.verdict == "APPROVE":
        if review.reply_text.strip() and review.reply_text.strip() != original_text.strip():
            review.verdict = "REWRITE"
            review.risk = "MEDIUM"
        else:
            review.verdict = "BLOCK"
            review.risk = "MEDIUM"
            review.quality_score = min(review.quality_score, 60)

    if high and review.verdict == "APPROVE":
        review.verdict = "BLOCK"
        if review.risk == "LOW":
            review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 55)

    role = str((context.get("agent") or {}).get("role") or "")
    state_corrected = _safe_state_correction(
        db,
        conversation_id=int(context.get("conversation_id") or 0),
        role=role,
        review=review,
        context=context,
    )

    if review.verdict == "CORRECT_STATE" and not state_corrected:
        # Nunca aplica estado inferido livremente pelo modelo.
        review.verdict = "BLOCK"
        review.reply_text = ""
        review.corrected_state = ""

    final_text = original_text
    final_buttons = list(original_buttons)

    if review.verdict in {"REWRITE", "CORRECT_STATE"}:
        candidate = review.reply_text.strip()
        if not candidate:
            review.verdict = "BLOCK"
        else:
            final_text = candidate
            labels = _buttons_from_labels(review.suggested_buttons)
            final_buttons = labels or _normalize_buttons(final_text, original_buttons)

    if review.verdict == "APPROVE":
        final_buttons = _normalize_buttons(original_text, original_buttons)

    should_send = review.verdict != "BLOCK"
    if review.verdict == "BLOCK":
        final_text = ""
        final_buttons = []

    return GuardianResult(
        verdict=review.verdict,
        risk=review.risk,
        confidence=review.confidence,
        quality_score=review.quality_score,
        issue_codes=review.issue_codes,
        rationale=review.rationale,
        reply_text=final_text,
        buttons=final_buttons,
        corrected_state=review.corrected_state,
        state_corrected=state_corrected,
        should_send=should_send,
    )


def _record_guardian_review(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    source: str,
    context: dict[str, Any],
    result: GuardianResult,
    original_text: str,
    original_buttons: list[dict[str, str]],
) -> None:
    write_audit(
        db,
        user=agent.user,
        action=GUARDIAN_ACTION,
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "conversation_id": conversation.id,
            "agent_id": agent.id,
            "agent_role": _role(agent),
            "source": source,
            "verdict": result.verdict,
            "risk": result.risk,
            "confidence": result.confidence,
            "quality_score": result.quality_score,
            "issue_codes": result.issue_codes,
            "rationale": result.rationale,
            "suggested_reply": result.reply_text if result.verdict != "APPROVE" else "",
            "suggested_state": result.corrected_state,
            "suggested_buttons": [item.get("label") for item in result.buttons],
            "original_reply": _redact_text(original_text),
            "original_buttons": _redact_object(original_buttons),
            "deterministic_findings": context.get("deterministic_findings") or [],
            "guardian": True,
            "shadow_only": False,
            "intervened": result.verdict != "APPROVE",
            "state_corrected": result.state_corrected,
        },
    )


def guard_outbound_message(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    reply_text: str,
    buttons: list[dict[str, str]] | None = None,
    source: str,
    proposed_state: str = "",
    metadata: dict[str, Any] | None = None,
) -> GuardianResult:
    original_text = str(reply_text or "").strip()
    original_buttons = _normalize_buttons(original_text, buttons)
    if not original_text:
        return GuardianResult(
            verdict="BLOCK",
            risk="HIGH",
            quality_score=0,
            rationale="Mensagem vazia não pode ser enviada.",
            reply_text="",
            buttons=[],
            should_send=False,
        )

    if not settings.ai_supervisor_guardian_enabled:
        return GuardianResult(
            verdict="APPROVE",
            risk="LOW",
            confidence=1.0,
            quality_score=100,
            rationale="Guardião desativado por feature flag.",
            reply_text=original_text,
            buttons=original_buttons,
            should_send=True,
        )

    context = build_guardian_context(
        db,
        agent=agent,
        conversation=conversation,
        reply_text=original_text,
        buttons=original_buttons,
        source=source,
        proposed_state=proposed_state,
        metadata=metadata,
    )

    try:
        if guardian_operational():
            review = _run_guardian_model(context)
        else:
            # Sem modelo, ainda aplicamos os guardrails objetivos.
            review = GuardianReview(
                verdict="APPROVE",
                risk="LOW",
                confidence=1.0,
                quality_score=100,
                rationale="Avaliação determinística sem modelo disponível.",
            )
        result = _apply_guardrails(
            db,
            review=review,
            context=context,
            original_text=original_text,
            original_buttons=original_buttons,
        )
        _record_guardian_review(
            db,
            agent=agent,
            conversation=conversation,
            source=source,
            context=context,
            result=result,
            original_text=original_text,
            original_buttons=original_buttons,
        )
        LOG.info(
            "ai_guardian conversation=%s source=%s role=%s verdict=%s risk=%s issues=%s",
            conversation.id,
            source,
            _role(agent),
            result.verdict,
            result.risk,
            ",".join(result.issue_codes) or "-",
        )
        return result
    except Exception as exc:
        # Disponibilidade: falha do modelo não derruba uma ação limpa. Achado
        # determinístico HIGH/CRITICAL, porém, continua fail-closed.
        _critical, high, codes = _severity_sets(context)
        fail_closed = bool(high)
        write_audit(
            db,
            user=agent.user,
            action=GUARDIAN_ERROR_ACTION,
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "conversation_id": conversation.id,
                "agent_id": agent.id,
                "agent_role": _role(agent),
                "source": source,
                "error": _redact_text(str(exc))[:1200],
                "issue_codes": sorted(codes),
                "fail_closed": fail_closed,
            },
        )
        LOG.exception(
            "ai_guardian_failed conversation=%s source=%s fail_closed=%s",
            conversation.id,
            source,
            fail_closed,
        )
        if fail_closed:
            return GuardianResult(
                verdict="BLOCK",
                risk="HIGH",
                confidence=1.0,
                quality_score=30,
                issue_codes=sorted(codes),
                rationale="Achado objetivo de alto risco bloqueado mesmo com falha do modelo.",
                reply_text="",
                buttons=[],
                should_send=False,
            )
        return GuardianResult(
            verdict="APPROVE",
            risk="MEDIUM",
            confidence=0.0,
            quality_score=70,
            rationale="Guardião indisponível; ação sem achado objetivo liberada em fail-open.",
            reply_text=original_text,
            buttons=original_buttons,
            should_send=True,
        )
