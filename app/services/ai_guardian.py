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
from app.services import ai_campaign_black as black_campaign
from app.services.ai_quick_replies import asks_for_free_text, quick_replies_for_message
from app.services.ai_supervisor import (
    _catalog_snapshot,
    _closing_context,
    _deterministic_findings,
    _normalized_similarity,
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
1.1. approved_catalog é a fonte única de preços. Um preço citado que seja igual ao
     price_with_fidelity ou ao price_without_fidelity do plano (residencial), ou ao price
     (empresa/Sulmóvel), e um plano oferecido numa cidade listada em cities, são fatos
     COMPROVADOS: não bloqueie, não reescreva para "o valor precisa ser confirmado" e não
     remova o preço. Só aponte erro de preço quando o valor for diferente do catálogo ou o
     plano não existir na cidade do cliente.
1.2. proposed_action.metadata.tool_facts traz os resultados das ferramentas oficiais chamadas
     nesta resposta (catálogo, calculadora de multa, manual aprovado). Valores e fatos que estão
     em tool_facts são COMPROVADOS: não os remova nem os troque por "não tenho essa informação".
2. SDR não coleta CPF, data de nascimento, CEP contratual, e-mail contratual, dados bancários
   ou assinatura. Intenção clara de compra deve seguir para o Vendedor IA.
2.1. Política comercial aprovada: assim que cidade e segmento estão confirmados, o SDR apresenta
     o maior plano disponível na cidade com preço e uma chamada direta para contratar (ex.: "Quer
     contratar esse plano agora?"). Isso é a oferta esperada, NÃO é confirmação de compra
     prematura. Também não bloqueie só porque intent/metadata do SDR (ex.: QUALIFYING) diverge
     do proposed_state; isso é rótulo interno e não chega ao cliente.
3. Vendedor IA continua exatamente do ponto atual do fechamento e não reabre prospecção.
4. Perguntas com 2 ou 3 respostas previsíveis devem usar botões/quick replies.
5. Não repita pergunta, oferta ou CTA recente sem mudança real de estratégia.
6. No funil sdrIaFunnel=true, não use etapas proibidas de ICCID, M2M, vencimento, taxa,
   unificação, metragem/NAP, anexos ou tipo de assinatura. Defaults/playbook do CRM prevalecem.
7. Nunca permita GANHO antes de assinatura confirmada pelo CRM.
7.1. Se o estado do closer já contém sign_url e a etapa proposta é WAITING_SIGNATURE,
     o contrato está pronto para ser enviado ao cliente. "Aguardando assinatura" é o
     status esperado nesse momento e NÃO é conflito nem pedido prematuro de assinatura.
7.2. Se proposed_action.metadata.crm_playbook_required=true, o próprio CRM acabou de
     informar que aquele campo continua pendente. Nesse caso, voltar à etapa indicada
     por crm_playbook_step NÃO é regressão: é recuperação autoritativa do fechamento.
     Isso só vale para o campo/patchKey explicitamente presentes no metadata atual.
7.3. Se proposed_action.metadata.authoritative_contract_knowledge=true, a mensagem foi
     construída deterministicamente a partir da base contratual oficial da Sulnet para
     responder uma dúvida objetiva do cliente. Preserve integralmente os fatos e a resposta
     original. Não troque a resposta por CTA, não mande consultar o contrato, não invente
     incerteza e não adicione botões apenas para retomar o playbook. O playbook permanece
     no mesmo estado e será retomado depois da resposta.
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


def guardian_enforcing() -> bool:
    """Modo ativo: pode reescrever ou bloquear antes do WhatsApp."""
    return bool(settings.ai_supervisor_guardian_enabled)


def guardian_shadowing() -> bool:
    """Modo sombra: revisa e audita sem alterar o envio."""
    return bool(
        settings.ai_supervisor_guardian_shadow_enabled
        and not guardian_enforcing()
    )


def guardian_operational() -> bool:
    return bool(
        (guardian_enforcing() or guardian_shadowing())
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
            "journey_state": proposed_state,
            "proposed_state": proposed_state,
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
    from agents import Agent
    from app.services.ai_runtime import run_agent_sync

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
    result = run_agent_sync(guardian, prompt)
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
    # Textos fixos da campanha Black são montados em código a partir das regras
    # do comercial: o modelo não pode reescrevê-los como "promoção não comprovada".
    if black_campaign.black_active() and original_text.strip() in {
        black_campaign.OFFER_QUESTION,
        black_campaign.OPTIONS_TEXT,
    }:
        return GuardianResult(
            verdict="APPROVE",
            risk="LOW",
            quality_score=100,
            rationale="Texto fixo da campanha Black aprovada pelo comercial.",
            reply_text=original_text,
            buttons=original_buttons,
            should_send=True,
        )

    critical, high, deterministic_codes = _severity_sets(context)
    review.issue_codes = list(
        dict.fromkeys(
            [str(item) for item in review.issue_codes if str(item)]
            + sorted(deterministic_codes)
        )
    )[:12]

    # Evidência objetiva de contrato pronto: contract/generate já devolveu um
    # signUrl e o closer está em WAITING_SIGNATURE. O modelo às vezes confunde
    # "aguardando assinatura" com "contrato não gerado"; essa interpretação não
    # pode bloquear um link real já persistido no estado.
    current_state = context.get("current_state") or {}
    closing = current_state.get("closing") or {}
    proposed = context.get("proposed_action") or {}
    signature_ready = bool(
        str((context.get("agent") or {}).get("role") or "").upper() == "CLOSER"
        and str(proposed.get("proposed_state") or "") == "WAITING_SIGNATURE"
        and closing.get("has_sign_url")
    )
    signature_false_positives = {
        "PREMATURE_SIGNATURE_REQUEST",
        "CONTRACT_STATUS_CONFLICT",
    }
    if signature_ready:
        model_codes = set(review.issue_codes) - deterministic_codes
        remaining_model_codes = model_codes - signature_false_positives
        if (
            model_codes
            and not remaining_model_codes
            and not high
            and review.verdict in {"BLOCK", "REWRITE", "CORRECT_STATE"}
        ):
            review.verdict = "APPROVE"
            review.risk = "LOW"
            review.quality_score = max(review.quality_score, 90)
            review.reply_text = ""
            review.corrected_state = ""
            review.rationale = (
                "signUrl válido já presente no estado; envio de assinatura "
                "é coerente com WAITING_SIGNATURE."
            )
        review.issue_codes = [
            code
            for code in review.issue_codes
            if code not in signature_false_positives
        ]

    # Regras do SDR (não pedir CPF, não fazer fechamento...) não valem para o
    # Vendedor IA: coletar dados é exatamente a etapa dele. Em 04/11 o Guardião
    # barrou a primeira pergunta do Vendedor com SDR_PROHIBITED_PII_COLLECTION
    # e o cliente ficou sem resposta depois do "Quero contratar".
    if str((context.get("agent") or {}).get("role") or "").upper() == "CLOSER":
        model_codes = set(review.issue_codes) - deterministic_codes
        sdr_only = {code for code in model_codes if code.upper().startswith("SDR_")}
        if sdr_only:
            review.issue_codes = [code for code in review.issue_codes if code not in sdr_only]
            if (
                not (model_codes - sdr_only)
                and not high
                and not critical
                and review.verdict in {"BLOCK", "REWRITE", "CORRECT_STATE"}
            ):
                review.verdict = "APPROVE"
                review.risk = "LOW"
                review.quality_score = max(review.quality_score, 90)
                review.reply_text = ""
                review.corrected_state = ""
                review.rationale = (
                    "Regras exclusivas do SDR não se aplicam ao Vendedor IA; "
                    "coleta de dados é a etapa do fechamento."
                )

    metadata = proposed.get("metadata") or {}
    sdr_closer_handoff = bool(
        str((context.get("agent") or {}).get("role") or "").upper() == "SDR"
        and str(metadata.get("handoff_target") or "") == "CLOSER_AI"
        and metadata.get("customer_commitment") is True
    )
    if sdr_closer_handoff and not critical and not high:
        review.verdict = "APPROVE"
        review.risk = "LOW"
        review.confidence = max(review.confidence, 0.95)
        review.quality_score = max(review.quality_score, 95)
        review.reply_text = ""
        review.suggested_buttons = []
        review.corrected_state = ""
        review.issue_codes = [
            code for code in review.issue_codes if code in deterministic_codes
        ]
        review.rationale = (
            "Intenção de compra confirmada: o SDR não enviará esta mensagem; "
            "a ação correta é o handoff operacional para o Vendedor IA."
        )
    authoritative_contract_knowledge = bool(
        str((context.get("agent") or {}).get("role") or "").upper() == "CLOSER"
        and metadata.get("authoritative_contract_knowledge") is True
    )

    # Dúvidas contratuais resolvidas pela base oficial são fatos autoritativos.
    # O Guardião continua podendo bloquear um risco objetivo HIGH/CRITICAL, mas
    # não pode substituir a resposta por CTA, incerteza ou leitura do contrato.
    if authoritative_contract_knowledge and not critical and not high:
        review.verdict = "APPROVE"
        review.risk = "LOW"
        review.confidence = max(review.confidence, 0.95)
        review.quality_score = max(review.quality_score, 95)
        review.reply_text = ""
        review.suggested_buttons = []
        review.corrected_state = ""
        review.issue_codes = [
            code for code in review.issue_codes if code in deterministic_codes
        ]
        review.rationale = (
            "Resposta contratual construída deterministicamente a partir da "
            "base oficial; preservar texto e fatos originais."
        )

    playbook_recovery = bool(
        str((context.get("agent") or {}).get("role") or "").upper() == "CLOSER"
        and metadata.get("crm_playbook_required") is True
        and str(metadata.get("crm_playbook_code") or "").strip()
        and str(metadata.get("crm_playbook_patch_key") or "").strip()
        and str(metadata.get("crm_playbook_step") or "").strip()
        == str(proposed.get("proposed_state") or "").strip()
    )
    playbook_false_positives = {
        "STATE_HISTORY_CONFLICT",
        "CLOSING_STEP_REGRESSION",
        "CLOSING_SEQUENCE_CONFLICT",
        "RECENT_QUESTION_REPEAT",
        "SDR_FUNNEL_PROHIBITED_STEP",
    }
    if playbook_recovery:
        model_codes = set(review.issue_codes) - deterministic_codes
        remaining_model_codes = model_codes - playbook_false_positives
        if (
            model_codes
            and not remaining_model_codes
            and not high
            and review.verdict in {"BLOCK", "REWRITE", "CORRECT_STATE"}
        ):
            review.verdict = "APPROVE"
            review.risk = "LOW"
            review.quality_score = max(review.quality_score, 90)
            review.reply_text = ""
            review.corrected_state = ""
            review.rationale = (
                "O playbook atual do CRM exige este campo; reposicionar para a "
                "etapa indicada pelo próprio CRM é recuperação válida do fechamento."
            )
        review.issue_codes = [
            code
            for code in review.issue_codes
            if code not in playbook_false_positives
        ]

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

    elif "STATE_REGRESSION" in high:
        # Estado regressivo não pode ser corrigido só trocando a frase.
        # O SDR precisa reconstruir a decisão com os fatos confirmados.
        review.verdict = "BLOCK"
        review.risk = "HIGH"
        review.quality_score = min(review.quality_score, 35)
        review.reply_text = ""

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

    # Pergunta que pede dado digitado (cidade, CEP, nome...) não leva botões,
    # nem os sugeridos pelo Guardião.
    if asks_for_free_text(final_text):
        final_buttons = []

    # A reescrita do próprio Guardião também precisa passar pelo anti-repetição.
    # Caso contrário ele poderia detectar repetição e devolver uma paráfrase
    # praticamente idêntica, que ainda assim chegaria ao cliente.
    if (
        review.verdict != "BLOCK"
        and final_text
        and not playbook_recovery
        and not authoritative_contract_knowledge
    ):
        prior_outbounds = [
            str(item.get("body") or "")
            for item in context.get("history") or []
            if str(item.get("direction") or "").upper() == "OUTBOUND"
        ]
        if any(
            _normalized_similarity(final_text, previous) >= 0.88
            for previous in prior_outbounds[-5:]
        ):
            review.verdict = "BLOCK"
            review.risk = "HIGH"
            review.quality_score = min(review.quality_score, 45)
            if "POSSIBLE_REPEAT" not in review.issue_codes:
                review.issue_codes.append("POSSIBLE_REPEAT")
            review.rationale = (
                review.rationale
                or "A saída final continuou repetitiva após a revisão do Guardião."
            )

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
    shadow_only: bool = False,
    observed_verdict: str | None = None,
) -> None:
    observed = observed_verdict or result.verdict
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
            "verdict": "APPROVE" if shadow_only else result.verdict,
            "observed_verdict": observed,
            "risk": result.risk,
            "confidence": result.confidence,
            "quality_score": result.quality_score,
            "issue_codes": result.issue_codes,
            "rationale": result.rationale,
            "suggested_reply": (
                _redact_text(result.reply_text)
                if observed != "APPROVE"
                else ""
            ),
            "suggested_state": result.corrected_state,
            "suggested_buttons": [item.get("label") for item in result.buttons],
            "original_reply": _redact_text(original_text),
            "original_buttons": _redact_object(original_buttons),
            "deterministic_findings": context.get("deterministic_findings") or [],
            "guardian": True,
            "shadow_only": shadow_only,
            "intervened": (not shadow_only) and result.verdict != "APPROVE",
            "would_intervene": observed != "APPROVE",
            "state_corrected": (not shadow_only) and result.state_corrected,
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

    enforcing = guardian_enforcing()
    shadowing = guardian_shadowing()
    if not enforcing and not shadowing:
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
    if shadowing:
        context = {**context, "mode": "GUARDIAN_SHADOW"}

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
        observed_verdict = result.verdict
        if shadowing:
            # Sombra nunca altera a saída: registra o que faria e libera o original.
            _record_guardian_review(
                db,
                agent=agent,
                conversation=conversation,
                source=source,
                context=context,
                result=result,
                original_text=original_text,
                original_buttons=original_buttons,
                shadow_only=True,
                observed_verdict=observed_verdict,
            )
            LOG.info(
                "ai_guardian_shadow conversation=%s source=%s role=%s observed=%s risk=%s issues=%s",
                conversation.id,
                source,
                _role(agent),
                observed_verdict,
                result.risk,
                ",".join(result.issue_codes) or "-",
            )
            return GuardianResult(
                verdict="APPROVE",
                risk=result.risk,
                confidence=result.confidence,
                quality_score=result.quality_score,
                issue_codes=list(result.issue_codes),
                rationale=(
                    f"[sombra] observou {observed_verdict}: {result.rationale}"
                    if result.rationale
                    else f"[sombra] observou {observed_verdict}"
                ),
                reply_text=original_text,
                buttons=original_buttons,
                corrected_state="",
                state_corrected=False,
                should_send=True,
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
            shadow_only=False,
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
        # determinístico HIGH/CRITICAL, porém, continua fail-closed — exceto
        # em sombra, onde nunca bloqueamos o envio.
        _critical, high, codes = _severity_sets(context)
        fail_closed = bool(high) and enforcing
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
                "shadow_only": shadowing,
            },
        )
        LOG.exception(
            "ai_guardian_failed conversation=%s source=%s fail_closed=%s shadow=%s",
            conversation.id,
            source,
            fail_closed,
            shadowing,
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
            rationale=(
                "Guardião em sombra indisponível; envio liberado."
                if shadowing
                else "Guardião indisponível; ação sem achado objetivo liberada em fail-open."
            ),
            reply_text=original_text,
            buttons=original_buttons,
            should_send=True,
        )
