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



def test_guardian_allows_real_sign_url_in_waiting_signature():
    review = GuardianReview(
        verdict="BLOCK",
        risk="HIGH",
        confidence=0.9,
        quality_score=55,
        issue_codes=[
            "CONTRACT_STATUS_CONFLICT",
            "PREMATURE_SIGNATURE_REQUEST",
        ],
        rationale="Contrato ainda não estaria pronto.",
    )
    context = _context([])
    context["current_state"] = {
        "closing": {
            "step": "WAITING_SIGNATURE",
            "has_sign_url": True,
            "contract_status": "aguardando_assinatura",
        }
    }
    context["proposed_action"] = {
        "proposed_state": "WAITING_SIGNATURE",
    }

    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text=(
            "Seu contrato está disponível para assinatura aqui: "
            "https://sign.example/abc"
        ),
        original_buttons=[],
    )

    assert result.verdict == "APPROVE"
    assert result.should_send is True
    assert result.risk == "LOW"
    assert "CONTRACT_STATUS_CONFLICT" not in result.issue_codes
    assert "PREMATURE_SIGNATURE_REQUEST" not in result.issue_codes



def test_guardian_allows_authoritative_crm_playbook_recovery():
    review = GuardianReview(
        verdict="BLOCK",
        risk="HIGH",
        confidence=0.9,
        quality_score=55,
        issue_codes=[
            "STATE_HISTORY_CONFLICT",
            "CLOSING_STEP_REGRESSION",
            "SDR_FUNNEL_PROHIBITED_STEP",
        ],
        rationale="Pareceria regressão sem o playbook atual.",
    )
    context = _context([], role="CLOSER")
    context["history"] = []
    context["proposed_action"] = {
        "proposed_state": "MOBILE_CONTRACT_TYPE",
        "metadata": {
            "closing_step": "MOBILE_CONTRACT_TYPE",
            "crm_playbook_required": True,
            "crm_playbook_step": "MOBILE_CONTRACT_TYPE",
            "crm_playbook_code": "cf_tipo_contratacao",
            "crm_playbook_patch_key": (
                "productInfo.categoryForm.values.tipo_contratacao"
            ),
        },
    }
    context["current_state"] = {"closing": {"step": "MOBILE_CONTRACT_TYPE"}}

    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text="Como você quer contratar o Sulmóvel?",
        original_buttons=[
            {"id": "portabilidade", "label": "Portabilidade"},
            {"id": "numero_novo", "label": "Número novo"},
        ],
    )

    assert result.verdict == "APPROVE"
    assert result.should_send is True
    assert result.risk == "LOW"
    assert "STATE_HISTORY_CONFLICT" not in result.issue_codes
    assert "CLOSING_STEP_REGRESSION" not in result.issue_codes
    assert "SDR_FUNNEL_PROHIBITED_STEP" not in result.issue_codes


def test_guardian_does_not_override_real_forbidden_sdr_closer_step():
    state = SimpleNamespace(step="MOBILE_PORT_ICCID", last_error="")

    class FakeDB:
        def scalar(self, _query):
            return state

    review = GuardianReview(
        verdict="APPROVE",
        risk="LOW",
        confidence=0.9,
        quality_score=95,
        issue_codes=["SDR_FUNNEL_PROHIBITED_STEP"],
    )
    context = _context(
        [
            {
                "code": "SDR_IA_FORBIDDEN_CLOSING_STEP",
                "severity": "CRITICAL",
                "detail": "Etapa proibida no funil SDR IA.",
            }
        ],
        role="CLOSER",
    )
    context["history"] = []
    context["proposed_action"] = {
        "proposed_state": "MOBILE_PORT_ICCID",
        "metadata": {
            "closing_step": "MOBILE_PORT_ICCID",
            "crm_playbook_required": True,
            "crm_playbook_step": "MOBILE_PORT_ICCID",
            "crm_playbook_code": "cf_port_iccid",
            "crm_playbook_patch_key": (
                "productInfo.categoryForm.values.port_iccid"
            ),
        },
    }
    context["current_state"] = {"closing": {"step": "MOBILE_PORT_ICCID"}}

    result = _apply_guardrails(
        FakeDB(),
        review=review,
        context=context,
        original_text="Qual é o ICCID?",
        original_buttons=[],
    )

    assert result.verdict == "CORRECT_STATE"
    assert result.state_corrected is True
    assert result.corrected_state == "WAITING_CRM_READY"
    assert state.step == "WAITING_CRM_READY"
    assert "SDR_IA_FORBIDDEN_CLOSING_STEP" in result.issue_codes


def test_guardian_preserves_authoritative_contract_knowledge():
    review = GuardianReview(
        verdict="REWRITE",
        risk="MEDIUM",
        confidence=0.88,
        quality_score=70,
        issue_codes=[
            "IGNORA_CONTEXTO_RECENTE",
            "CTA_REPETIDO",
            "PLAYBOOK_PENDENTE",
        ],
        rationale="Modelo tentou retomar o playbook.",
        reply_text=(
            "Não tenho a fidelidade confirmada nas informações disponíveis. "
            "Confirme no contrato."
        ),
        suggested_buttons=["Sim", "Não"],
    )
    context = _context([], role="CLOSER")
    context["history"] = []
    context["proposed_action"] = {
        "proposed_state": "WAITING_SIGNATURE",
        "metadata": {
            "closing_step": "WAITING_SIGNATURE",
            "authoritative_contract_knowledge": True,
            "knowledge_source": "FTTH_CONTRACT_POLICY",
        },
    }
    context["current_state"] = {
        "closing": {
            "step": "WAITING_SIGNATURE",
            "has_sign_url": True,
        }
    }

    original = (
        "Para o plano residencial de 800 Mega, a fidelidade/permanência "
        "é de 12 meses."
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text=original,
        original_buttons=[],
    )

    assert result.verdict == "APPROVE"
    assert result.should_send is True
    assert result.reply_text == original
    assert result.buttons == []
    assert result.risk == "LOW"
    assert "PLAYBOOK_PENDENTE" not in result.issue_codes
