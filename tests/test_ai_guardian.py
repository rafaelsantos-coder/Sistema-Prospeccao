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



def test_guardian_keeps_authoritative_sdr_catalog_script():
    review = GuardianReview(
        verdict="REWRITE",
        risk="MEDIUM",
        confidence=0.8,
        quality_score=60,
        issue_codes=["POSSIBLE_REPEAT"],
        rationale="Trocaria o menu por um único plano.",
        reply_text=(
            "Para usar só móvel, o plano com mais internet é o Sulmóvel 15GB "
            "por R$ 69,80. Quer contratar esse plano agora?"
        ),
        suggested_buttons=["Sim, quero contratar", "Quero conhecer opções"],
    )
    context = _context([], role="SDR")
    context["proposed_action"] = {
        "proposed_state": "OFFERING",
        "metadata": {"authoritative_sdr_script": True},
    }
    original = "No Sulmóvel temos planos nacionais com valores confirmados. Escolha uma opção para continuar:"
    buttons = [
        {"id": "mobile_2gb", "label": "2GB - R$ 33,90"},
        {"id": "mobile_5gb", "label": "5GB - R$ 44,90"},
        {"id": "mobile_mais", "label": "Mais planos"},
    ]

    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text=original,
        original_buttons=buttons,
    )

    assert result.verdict == "APPROVE"
    assert result.should_send is True
    assert result.reply_text == original
    assert [item["label"] for item in result.buttons] == [
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
        "Mais planos",
    ]


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


def test_guardian_approves_operational_sdr_to_closer_handoff():
    review = GuardianReview(
        verdict="BLOCK",
        risk="HIGH",
        confidence=0.9,
        quality_score=40,
        issue_codes=[
            "SDR_INTENTAO_DE_COMPRA_HANDOFF",
            "ETAPA_INCORRETA",
            "CTA_REPETIDO",
        ],
        rationale="Modelo tentou bloquear a fala do SDR antes do handoff.",
    )
    context = _context([], role="SDR")
    context["history"] = []
    context["proposed_action"] = {
        "proposed_state": "CLOSING",
        "metadata": {
            "intent": "HIGH_INTENT",
            "handoff_target": "CLOSER_AI",
            "customer_commitment": True,
        },
    }

    result = _apply_guardrails(
        None,
        review=review,
        context=context,
        original_text=(
            "Perfeito. O Vendedor IA dará continuidade à contratação."
        ),
        original_buttons=[],
    )

    assert result.verdict == "APPROVE"
    assert result.should_send is True
    assert result.risk == "LOW"
    assert result.issue_codes == []


def test_guardian_nao_aplica_regras_do_sdr_ao_vendedor_ia():
    # 04/11, conversa 459: a primeira pergunta do Vendedor IA foi barrada
    # como se fosse o SDR pedindo dado pessoal; o cliente ficou sem resposta.
    review = GuardianReview(
        verdict="BLOCK",
        risk="HIGH",
        confidence=0.9,
        quality_score=30,
        issue_codes=["SDR_PROHIBITED_PII_COLLECTION", "SDR_FUNNEL_CLOSING_STEP"],
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([], role="CLOSER"),
        original_text="Perfeito! Para começar, me confirma seu nome completo?",
        original_buttons=[],
    )
    assert result.should_send is True
    assert result.verdict == "APPROVE"
    assert not [code for code in result.issue_codes if code.startswith("SDR_")]


def test_guardian_mantem_bloqueio_do_vendedor_com_outro_motivo():
    review = GuardianReview(
        verdict="BLOCK",
        risk="HIGH",
        confidence=0.9,
        quality_score=30,
        issue_codes=["SDR_FUNNEL_CLOSING_STEP", "CUSTOMER_DECLINED"],
    )
    result = _apply_guardrails(
        None,
        review=review,
        context=_context([], role="CLOSER"),
        original_text="Qual é o seu CPF?",
        original_buttons=[],
    )
    assert result.should_send is False
    assert "CUSTOMER_DECLINED" in result.issue_codes


def test_guardian_shadow_observes_block_but_still_sends(monkeypatch):
    from dataclasses import replace

    from app.config import settings
    from app.services import ai_guardian

    audits: list[dict] = []

    monkeypatch.setattr(
        ai_guardian,
        "settings",
        replace(
            settings,
            ai_supervisor_guardian_enabled=False,
            ai_supervisor_guardian_shadow_enabled=True,
            openai_api_key="sk-test",
        ),
    )
    monkeypatch.setattr(
        ai_guardian,
        "build_guardian_context",
        lambda *args, **kwargs: _context(
            [
                {
                    "code": "WON_BEFORE_SIGNATURE",
                    "severity": "CRITICAL",
                    "detail": "Venda marcada como ganha antes da assinatura.",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ai_guardian,
        "_run_guardian_model",
        lambda _context: GuardianReview(
            verdict="APPROVE",
            risk="LOW",
            confidence=0.9,
            quality_score=95,
        ),
    )
    monkeypatch.setattr(
        ai_guardian,
        "write_audit",
        lambda db, user=None, action="", entity_type="", entity_id=None, payload=None: audits.append(
            {"action": action, "payload": payload or {}}
        ),
    )

    agent = SimpleNamespace(id=7, user=None, role_type="CLOSER")
    conversation = SimpleNamespace(id=42)
    result = ai_guardian.guard_outbound_message(
        None,
        agent=agent,
        conversation=conversation,
        reply_text="Sua contratação foi concluída.",
        buttons=[],
        source="CLOSER_TEST",
    )

    assert result.should_send is True
    assert result.verdict == "APPROVE"
    assert result.reply_text == "Sua contratação foi concluída."
    assert "WON_BEFORE_SIGNATURE" in result.issue_codes
    assert audits
    payload = audits[0]["payload"]
    assert payload["shadow_only"] is True
    assert payload["intervened"] is False
    assert payload["would_intervene"] is True
    assert payload["observed_verdict"] == "BLOCK"
    assert payload["verdict"] == "APPROVE"


def test_guardian_shadow_disabled_skips_review(monkeypatch):
    from dataclasses import replace

    from app.config import settings
    from app.services import ai_guardian

    called = {"model": False}

    monkeypatch.setattr(
        ai_guardian,
        "settings",
        replace(
            settings,
            ai_supervisor_guardian_enabled=False,
            ai_supervisor_guardian_shadow_enabled=False,
            openai_api_key="sk-test",
        ),
    )
    monkeypatch.setattr(
        ai_guardian,
        "_run_guardian_model",
        lambda _context: called.__setitem__("model", True) or GuardianReview(),
    )

    agent = SimpleNamespace(id=7, user=None, role_type="SDR")
    conversation = SimpleNamespace(id=42)
    result = ai_guardian.guard_outbound_message(
        None,
        agent=agent,
        conversation=conversation,
        reply_text="Olá!",
        buttons=[],
        source="SDR_TEST",
    )

    assert result.should_send is True
    assert result.verdict == "APPROVE"
    assert called["model"] is False
    assert "desativado" in result.rationale.casefold()
