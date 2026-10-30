from __future__ import annotations

from types import SimpleNamespace

from app.services.ai_supervisor import (
    SupervisorReview,
    _apply_shadow_guardrails,
    _closed_choice_question,
    _deterministic_findings,
    _redact_text,
)


def _run(reply: str = ""):
    return SimpleNamespace(
        id=101,
        conversation_id=202,
        reply_text=reply,
    )


def test_shadow_detects_closed_choice_without_buttons():
    reply = "Você prefere boleto ou débito em conta?"
    findings = _deterministic_findings(
        role="CLOSER",
        run=_run(reply),
        run_output={"quick_replies": []},
        history=[
            {"id": 1, "direction": "INBOUND", "body": "Quero contratar."},
        ],
        actual_outbound=SimpleNamespace(id=2, message_type="TEXT"),
        closing={"step": "BILLING_PAYMENT_METHOD", "sdr_ia_funnel": True},
        ai_state=SimpleNamespace(state="CLOSING"),
        lead_stage="NEGOCIACAO",
    )

    codes = {item["code"] for item in findings}
    assert "MISSING_QUICK_REPLIES" in codes
    assert _closed_choice_question(reply) is True


def test_shadow_detects_sdr_collecting_closing_data():
    findings = _deterministic_findings(
        role="SDR",
        run=_run("Para continuar, qual seu CPF?"),
        run_output={"quick_replies": []},
        history=[],
        actual_outbound=SimpleNamespace(id=2, message_type="TEXT"),
        closing={},
        ai_state=SimpleNamespace(state="AI_ACTIVE"),
        lead_stage="PROPOSTA",
    )

    codes = {item["code"] for item in findings}
    assert "SDR_COLLECTING_CLOSING_DATA" in codes


def test_shadow_detects_won_before_signature_as_critical():
    findings = _deterministic_findings(
        role="CLOSER",
        run=_run("Sua contratação foi concluída."),
        run_output={},
        history=[],
        actual_outbound=SimpleNamespace(id=2, message_type="TEXT"),
        closing={
            "step": "COMPLETED",
            "contract_status": "aguardando_assinatura",
            "sdr_ia_funnel": True,
        },
        ai_state=SimpleNamespace(state="COMPLETED"),
        lead_stage="GANHO",
    )

    critical = next(
        item for item in findings
        if item["code"] == "WON_BEFORE_SIGNATURE"
    )
    assert critical["severity"] == "CRITICAL"


def test_shadow_detects_state_regression_after_customer_answered_no():
    findings = _deterministic_findings(
        role="SDR",
        run=_run("Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"),
        run_output={
            "quick_replies": [{"id": "sim", "label": "Sim"}, {"id": "nao", "label": "Não"}],
            "journey_state": "IDENTIFY_CUSTOMER",
        },
        history=[
            {
                "id": 1,
                "direction": "OUTBOUND",
                "body": "Você já possui algum serviço da Sulnet?",
            },
            {"id": 2, "direction": "INBOUND", "body": "Não"},
            {
                "id": 3,
                "direction": "OUTBOUND",
                "body": "Internet ou Telefonia Móvel?",
            },
            {"id": 4, "direction": "INBOUND", "body": "Internet"},
        ],
        actual_outbound=None,
        closing={},
        ai_state=SimpleNamespace(state="AI_ACTIVE"),
        lead_stage="CONTATO_REALIZADO",
    )

    by_code = {item["code"]: item for item in findings}
    assert by_code["STATE_REGRESSION"]["severity"] == "HIGH"
    assert by_code["IGNORED_CONFIRMED_ANSWER"]["severity"] == "HIGH"


def test_shadow_marks_near_duplicate_as_high_risk():
    findings = _deterministic_findings(
        role="SDR",
        run=_run("Você já possui algum serviço da Sulnet?"),
        run_output={
            "quick_replies": [{"id": "sim", "label": "Sim"}, {"id": "nao", "label": "Não"}],
            "journey_state": "IDENTIFY_CUSTOMER",
        },
        history=[
            {
                "id": 1,
                "direction": "OUTBOUND",
                "body": "Você já possui algum serviço da Sulnet?",
            }
        ],
        actual_outbound=None,
        closing={},
        ai_state=SimpleNamespace(state="AI_ACTIVE"),
        lead_stage="CONTATO_REALIZADO",
    )

    repeat = next(item for item in findings if item["code"] == "POSSIBLE_REPEAT")
    assert repeat["severity"] == "HIGH"


def test_shadow_guardrails_can_upgrade_model_approval_to_block():
    context = {
        "deterministic_findings": [
            {
                "code": "WON_BEFORE_SIGNATURE",
                "severity": "CRITICAL",
                "detail": "Venda ganha sem assinatura.",
            }
        ]
    }
    review = SupervisorReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.8,
        quality_score=95,
        rationale="Parecia correto.",
    )

    guarded = _apply_shadow_guardrails(review, context)

    assert guarded.verdict == "BLOCK"
    assert guarded.risk == "CRITICAL"
    assert guarded.quality_score <= 35
    assert "WON_BEFORE_SIGNATURE" in guarded.issue_codes
    assert guarded.checks.crm_playbook_violation is True


def test_shadow_guardrails_can_upgrade_high_risk_approval_to_correct():
    context = {
        "deterministic_findings": [
            {
                "code": "MISSING_QUICK_REPLIES",
                "severity": "HIGH",
                "detail": "Pergunta fechada sem botão.",
            }
        ]
    }
    review = SupervisorReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.9,
        quality_score=92,
    )

    guarded = _apply_shadow_guardrails(review, context)

    assert guarded.verdict == "CORRECT"
    assert guarded.risk == "HIGH"
    assert guarded.quality_score <= 65
    assert guarded.checks.missing_quick_replies is True


def test_shadow_redacts_personal_data_before_model_review():
    raw = (
        "CPF 11111111111, CEP 98900000 e e-mail pessoa@exemplo.com. "
        "Bearer token-super-secreto"
    )
    redacted = _redact_text(raw)

    assert "11111111111" not in redacted
    assert "98900000" not in redacted
    assert "pessoa@exemplo.com" not in redacted
    assert "token-super-secreto" not in redacted
    assert "[DOCUMENTO]" in redacted
    assert "[CEP]" in redacted
    assert "[EMAIL]" in redacted
