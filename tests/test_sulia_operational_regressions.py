"""Regressões dos cinco bloqueios operacionais: recusas, follow-up, handoff e CRM."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models import AIAgentProfile, Conversation
from app.services.ai_followups import (
    _GUARDIAN_OPT_OUT_ISSUES,
    _NO_CONTACT_RE,
    _normalize_followup_text,
    _persist_explicit_no_contact,
    schedule_followup,
)
from app.services.ai_seller import (
    PLAN_CHANGE_STEP,
    _handle_sale_adjustment,
    _hold_status_poll_failure,
)
from app.services.crm_sulnet import CRMIntegrationError


def _sale():
    return SimpleNamespace(
        plan_label="Fibra 800 Mega",
        offer_price=Decimal("119.90"),
        step="SALE_ADJUST",
        opportunity_id=11819,
        collected_json="{}",
        updated_at=None,
    )


def test_price_question_preserves_closer_owner_and_promotion():
    state = _sale()
    collected = {"freeMonths": 3, "_sale_intent_confirmed": True}
    reply = _handle_sale_adjustment(
        None,
        state=state,
        conversation=SimpleNamespace(id=448),
        collected=collected,
        text="Qual é o valor mensal da internet?",
    )
    assert state.step == "SALE_ADJUST"
    assert "R$ 119,90" in reply
    assert collected["freeMonths"] == 3
    assert collected["_sale_intent_confirmed"]


def test_explicit_plan_change_is_only_case_transferring_ownership():
    state = _sale()
    collected = {"freeMonths": 3}
    _handle_sale_adjustment(
        None,
        state=state,
        conversation=SimpleNamespace(id=3010),
        collected=collected,
        text="Quero trocar o plano de internet",
    )
    assert state.step == PLAN_CHANGE_STEP
    assert collected["_explicit_plan_change"] is True
    assert collected["freeMonths"] == 3


def test_do_not_contact_pattern_does_not_confuse_plan_rejection():
    assert _NO_CONTACT_RE.search(_normalize_followup_text("não me mande mais ofertas"))
    assert _NO_CONTACT_RE.search(_normalize_followup_text("retire meu número da lista"))
    assert not _NO_CONTACT_RE.search(_normalize_followup_text("não quero esse plano"))
    assert not _NO_CONTACT_RE.search(_normalize_followup_text("prefiro 650 Mega"))


def test_guardian_optout_issues_trigger_hard_stop():
    assert {"IGNORES_EXPLICIT_OPT_OUT", "CUSTOMER_OPTED_OUT"} <= _GUARDIAN_OPT_OUT_ISSUES


def test_optout_persists_lead_and_conversation_without_sending():
    lead = SimpleNamespace(
        do_not_contact=False, stage="PROPOSTA", next_action_at="scheduled",
    )
    conversation = SimpleNamespace(
        lead=lead, status="EM_ATENDIMENTO", closed_at=None,
    )
    state = SimpleNamespace(state="AI_ACTIVE", last_intent="UNKNOWN")
    db = MagicMock()
    db.scalar.side_effect = ["Não me envie mais mensagens", state]
    db.get.return_value = conversation
    with patch("app.services.ai_followups.cancel_pending_followups", return_value=2):
        assert _persist_explicit_no_contact(db, 2661)
    assert lead.do_not_contact is True
    assert lead.stage == "NAO_CONTATAR"
    assert lead.next_action_at is None
    assert state.state == "OPT_OUT"
    assert state.last_intent == "DO_NOT_CONTACT"
    assert conversation.status == "ENCERRADO"
    assert conversation.closed_at is not None


def test_followup_not_scheduled_when_limit_reached():
    db = MagicMock()
    conversation = SimpleNamespace(status="EM_ATENDIMENTO", lead_id=None)
    agent = SimpleNamespace(role_type="SDR")
    db.get.side_effect = lambda model, _id: (
        conversation if model is Conversation else agent if model is AIAgentProfile else None
    )
    db.scalar.return_value = None
    with (
        patch("app.services.ai_followups._customer_declined", return_value=False),
        patch("app.services.ai_followups._queue_ai_paused", return_value=False),
        patch("app.services.ai_followups._followup_allowed", return_value=(False, "LIMITE_SEM_RESPOSTA")),
    ):
        scheduled = schedule_followup(
            db, agent_id=1, queue_id=1, conversation_id=1337,
            source_message_id=12001, step=3,
        )
    assert scheduled is None
    db.add.assert_not_called()


def test_missing_crm_opportunity_alerted_on_first_404_and_held():
    state = _sale()
    state.step = "WAITING_SIGNATURE"
    state.id = 4
    state.agent_id = 2
    state.external_id = "prospecta-conversation-200"
    db = MagicMock()
    collected = {}
    exc = CRMIntegrationError(
        "Lead não encontrada", status_code=404,
        method="GET", path="/api/capture/leads/11819/status",
    )
    with (
        patch("app.services.ai_seller._load_collected", return_value=collected),
        patch("app.services.ai_seller._save_collected"),
        patch("app.services.ai_seller._record_ai_block") as alert,
    ):
        held = _hold_status_poll_failure(
            db,
            state=state,
            conversation=SimpleNamespace(id=200),
            agent=SimpleNamespace(id=2),
            exc=exc,
            step="WAITING_SIGNATURE",
        )
    assert held is True
    assert collected["_crm_contract_reconciliation_pending"] is True
    assert collected["_crm_contract_missing_opportunity"] == 11819
    assert collected["_status_poll_failures"] == 1
    alert.assert_called_once()


def test_transient_crm_error_does_not_mark_contract_as_won():
    state = _sale()
    state.step = "WAITING_SIGNATURE"
    collected = {}
    exc = CRMIntegrationError("temporário", status_code=503)
    with (
        patch("app.services.ai_seller._load_collected", return_value=collected),
        patch("app.services.ai_seller._save_collected"),
        patch("app.services.ai_seller._record_ai_block") as alert,
    ):
        held = _hold_status_poll_failure(
            MagicMock(), state=state,
            conversation=SimpleNamespace(id=200),
            agent=SimpleNamespace(id=2), exc=exc,
            step="WAITING_SIGNATURE",
        )
    assert held is False
    assert state.step == "WAITING_SIGNATURE"
    assert "_crm_contract_reconciliation_pending" not in collected
    alert.assert_not_called()
