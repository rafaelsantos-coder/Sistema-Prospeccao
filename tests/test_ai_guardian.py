from __future__ import annotations

from types import SimpleNamespace

from app.services.ai_guardian import (
    GuardianReview,
    _apply_guardrails,
)


def _context(findings: list[dict], *, role: str = "CLOSER") -> dict:
    return {
        "conversation_id": 101,
        "agent": {"role": role},
        "deterministic_findings": findings,
    }


def test_guardian_blocks_won_before_signature_even_if_model_approves():
    review = GuardianReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.95,
        quality_score=96,
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([
            {
                "code": "WON_BEFORE_SIGNATURE",
                "severity": "CRITICAL",
                "detail": "Venda marcada como ganha antes da assinatura.",
            }
        ]),
        original_text="Sua contratação foi concluída.",
        original_buttons=[],
    )

    assert result.verdict == "BLOCK"
    assert result.should_send is False
    assert result.risk == "CRITICAL"
    assert "WON_BEFORE_SIGNATURE" in result.issue_codes


def test_guardian_rewrites_closed_choice_when_it_can_supply_buttons():
    review = GuardianReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.9,
        quality_score=90,
        reply_text="Você prefere boleto ou débito em conta?",
        suggested_buttons=["Boleto", "Débito em conta"],
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([
            {
                "code": "MISSING_QUICK_REPLIES",
                "severity": "HIGH",
                "detail": "Pergunta fechada sem botões.",
            }
        ]),
        original_text="Você prefere boleto ou débito em conta?",
        original_buttons=[],
    )

    assert result.verdict == "REWRITE"
    assert result.should_send is True
    assert [item["label"] for item in result.buttons] == [
        "Boleto",
        "Débito em conta",
    ]


def test_guardian_blocks_sdr_collecting_closing_data():
    review = GuardianReview(
        verdict="REWRITE",
        risk="MEDIUM",
        confidence=0.8,
        quality_score=70,
        reply_text="Pode me informar seu CPF?",
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([
            {
                "code": "SDR_COLLECTING_CLOSING_DATA",
                "severity": "HIGH",
                "detail": "SDR tentou coletar CPF.",
            }
        ], role="SDR"),
        original_text="Pode me informar seu CPF?",
        original_buttons=[],
    )

    assert result.verdict == "BLOCK"
    assert result.should_send is False
    assert "SDR_COLLECTING_CLOSING_DATA" in result.issue_codes


def test_guardian_blocks_state_regression_even_if_model_wants_to_rewrite():
    review = GuardianReview(
        verdict="REWRITE",
        risk="MEDIUM",
        confidence=0.9,
        quality_score=75,
        reply_text="A internet é para sua casa ou empresa?",
        suggested_buttons=["Para casa", "Para empresa"],
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([
            {
                "code": "STATE_REGRESSION",
                "severity": "HIGH",
                "detail": "Tentou voltar para IDENTIFY_CUSTOMER.",
            }
        ], role="SDR"),
        original_text="Você já possui algum serviço da Sulnet?",
        original_buttons=[],
    )

    assert result.verdict == "BLOCK"
    assert result.should_send is False
    assert "STATE_REGRESSION" in result.issue_codes


def test_guardian_blocks_rewrite_that_is_still_near_duplicate():
    context = _context([
        {
            "code": "POSSIBLE_REPEAT",
            "severity": "HIGH",
            "detail": "Resposta repetitiva.",
        }
    ], role="SDR")
    context["history"] = [
        {
            "direction": "OUTBOUND",
            "body": "Você já possui algum serviço da Sulnet?",
        }
    ]
    review = GuardianReview(
        verdict="REWRITE",
        risk="HIGH",
        confidence=0.9,
        quality_score=70,
        reply_text="Você já possui algum serviço da Sulnet?",
        suggested_buttons=["Sim", "Não"],
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text="Só para eu direcionar: você já possui algum serviço da Sulnet?",
        original_buttons=[],
    )

    assert result.verdict == "BLOCK"
    assert result.should_send is False
    assert "POSSIBLE_REPEAT" in result.issue_codes


def test_guardian_only_corrects_known_forbidden_closer_state(monkeypatch):
    state = SimpleNamespace(step="MOBILE_NEW_ICCID", last_error="erro anterior")

    class FakeDB:
        def scalar(self, _query):
            return state

    review = GuardianReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.9,
        quality_score=90,
    )
    result = _apply_guardrails(
        FakeDB(),
        review=review,
        context=_context([
            {
                "code": "SDR_IA_FORBIDDEN_CLOSING_STEP",
                "severity": "CRITICAL",
                "detail": "Etapa proibida no funil SDR IA.",
            }
        ]),
        original_text="Vou continuar sua contratação.",
        original_buttons=[],
    )

    assert result.verdict == "CORRECT_STATE"
    assert result.state_corrected is True
    assert result.corrected_state == "WAITING_CRM_READY"
    assert state.step == "WAITING_CRM_READY"
    assert state.last_error == ""
