import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.models import (
    Activity,
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIQueueBinding,
    AIReplyDelivery,
    Contact,
    Conversation,
    Lead,
    ServiceQueue,
)
from app.services.ai_reply_delivery import process_next_ai_reply, queue_ai_reply, reply_statuses
from app.services.ai_sdr import _official_catalog_for_context, enqueue_ai_job, process_ai_job, run_ai_decision
from app.services.ai_sdr_catalog_reply import accepted_black_quote, contextual_catalog_reply
from app.services.ai_sdr_policies import (
    _confirmed_facts_from_history,
    _derive_journey_state,
    _fast_purchase_decision,
    _fast_purchase_request,
)
from app.services.ai_service_handoff import service_handoff
from app.services.conversation_engine import register_message, update_message_statuses
from app.services.crm_notes import has_confirmed_activation
from app.services.zapi import ZApiError
from app.timeutils import utcnow


@pytest.fixture
def chat(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.ai_sdr.settings", replace(
            settings, ai_sdr_enabled=True, ai_seller_enabled=False, ai_autonomous_enabled=True,
        ),
    )
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
        contact = Contact(phone="555500000001")
        queue = ServiceQueue(name="Fila IA", code="FILA-IA", team="DIGITAL")
        agent = AIAgentProfile(user_id=1, slug="sulia-teste", execution_mode="AUTO")
        db.add_all([contact, queue, agent])
        db.flush()
        conversation = Conversation(contact_id=contact.id, team="DIGITAL")
        db.add(conversation)
        db.flush()
        db.add(AIQueueBinding(queue_id=queue.id, agent_id=agent.id, handling_mode="AI_FIRST"))
        monkeypatch.setattr("app.services.ai_queue_scope.ai_allowed_for_conversation", lambda *a, **k: True)
        monkeypatch.setattr("app.services.ai_sdr.ai_allowed_for_conversation", lambda *a, **k: True)
        monkeypatch.setattr("app.services.ai_sdr.binding_for_conversation", lambda *_: SimpleNamespace(
            queue_id=queue.id, agent_id=agent.id, agent=agent, handling_mode="AI_FIRST",
        ))
        db.commit()
        yield db, conversation, agent, queue
    engine.dispose()


def _inbound(db, conversation, text):
    message = register_message(
        db, conversation, direction="INBOUND", message_type="TEXT", body=text, status="RECEIVED",
    )
    db.flush()
    return message


def test_menu_after_human_pauses_jobs_and_followups_then_resumes(chat):
    db, conversation, agent, queue = chat
    original = _inbound(db, conversation, "Quero saber os planos")
    job = enqueue_ai_job(db, conversation=conversation, message=original)
    assert job
    followup = AIFollowupJob(
        conversation_id=conversation.id, agent_id=agent.id, queue_id=queue.id, source_message_id=original.id,
        due_at=utcnow(),
    )
    db.add(followup)
    db.flush()

    automatic = _inbound(db, conversation, "Opção de menu inválida.")
    assert enqueue_ai_job(db, conversation=conversation, message=automatic) is None
    assert followup.status == "CANCELADO"
    assert process_ai_job(db, job) == "skipped"
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Aguardando pessoa"

    person = _inbound(db, conversation, "Quero um plano de fibra")
    assert enqueue_ai_job(db, conversation=conversation, message=person)


def test_response_retries_only_confirmed_failures_and_preserves_buttons(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero conhecer os planos")
    buttons = [{"id": "fibra", "label": "Fibra"}, {"id": "movel", "label": "Móvel"}]
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Qual produto?", buttons=buttons,
    )
    assert queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Resposta duplicada",
    ).id == pending.id
    db.commit()
    delivery = db.scalar(select(AIReplyDelivery))
    attempts = []

    def send(**payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise ZApiError("Limite temporário.", status_code=429)
        return {"messageId": "sulia-reply"}

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_button_list=send),
    )
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Aguardando IA"
    assert process_next_ai_reply(db)
    assert pending.status == "FAILED"
    assert reply_statuses(db, [conversation.id])[conversation.id]["attention"]
    assert not process_next_ai_reply(db)
    delivery.available_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert process_next_ai_reply(db)
    assert pending.status == "SENT"
    assert attempts[0] == attempts[1]
    assert attempts[1]["buttons"] == buttons
    assert db.scalar(select(func.count()).select_from(AIReplyDelivery)) == 1

    update_message_statuses(db, {"status": "RECEIVED", "ids": ["sulia-reply"]})
    db.commit()
    update_message_statuses(db, {"status": "SENT", "ids": ["sulia-reply"]})
    db.commit()
    assert reply_statuses(db, [conversation.id])[conversation.id]["label"] == "Entregue"
    assert not process_next_ai_reply(db)


def test_missing_http_confirmation_never_retries_automatically(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero saber o preço")
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Qual a sua cidade?",
    )
    db.commit()
    attempts = []

    def send(**payload):
        attempts.append(payload)
        raise ZApiError("Confirmação indisponível.") from httpx.ReadTimeout("timeout")

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_text=send),
    )
    assert process_next_ai_reply(db)
    assert pending.status == "UNKNOWN"
    delivery = db.scalar(select(AIReplyDelivery))
    delivery.available_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert not process_next_ai_reply(db)
    assert len(attempts) == 1


def test_provider_callback_can_precede_send_result(chat, monkeypatch):
    db, conversation, agent, _ = chat
    original = _inbound(db, conversation, "Quero ver as opções")
    pending = queue_ai_reply(
        db, conversation=conversation, agent=agent, inbound_id=original.id, text="Você prefere fibra ou móvel?",
    )
    db.commit()

    def send(**_payload):
        update_message_statuses(db, {"status": "READ", "ids": ["early-id"]})
        db.flush()
        return {"messageId": "early-id"}

    monkeypatch.setattr(
        "app.services.zapi_instances.service_for_conversation", lambda *_: SimpleNamespace(send_text=send),
    )
    assert process_next_ai_reply(db)
    assert pending.status == "READ"
    assert pending.delivered_at
    assert pending.read_at


@pytest.mark.parametrize(("text", "offer", "expected"), [
    ("Quero contratar", "Em Santa Rosa temos fibra de 800 Mega por R$ 119,90. Quer contratar?", "FTTH_800"),
    ("Sim", "Sulmóvel 5GB por R$ 44,90. Quer contratar agora?", "SULMOVEL_5GB"),
    ("Quero contratar", "650 Mega por R$ 99,90 ou 800 Mega por R$ 119,90.", None),
    ("Quero contratar se for mais barato", "Fibra de 800 Mega por R$ 119,90.", None),
    ("Sim", "Temos fibra de 800 Mega por R$ 119,90. Você já é cliente?", None),
])
def test_purchase_acceptance_uses_the_offer_the_customer_received(text, offer, expected):
    request = _fast_purchase_request({
        "latest_customer_message": text,
        "history": [{"direction": "OUTBOUND", "body": offer}, {"direction": "INBOUND", "body": text}],
        "confirmed_facts": {"city": "Santa Rosa"},
    })
    if expected is None:
        assert request is None
    else:
        assert request["plan"]["code"] == expected
        decision = _fast_purchase_decision(request["plan"], request["city"], handoff=True)
        assert decision.customer_commitment and decision.handoff_target == "CLOSER_AI"


def test_acceptance_without_plan_in_latest_message_bypasses_model(chat, monkeypatch):
    db, conversation, agent, queue = chat
    register_message(
        db, conversation, direction="OUTBOUND", message_type="TEXT",
        body="Em Santa Rosa temos fibra de 800 Mega por R$ 119,90. Quer contratar agora?",
        sender_user_id=agent.user_id, status="SENT",
    )
    original = _inbound(db, conversation, "Quero contratar")
    job = AIJob(conversation_id=conversation.id, message_id=original.id, agent_id=agent.id, queue_id=queue.id)
    db.add(job)
    db.flush()
    observed = []
    monkeypatch.setattr(
        "app.services.ai_sdr.build_ai_context",
        lambda *_a, **_k: {
            "latest_customer_message": original.body,
            "history": [
                {"direction": "OUTBOUND", "body": "Fibra de 800 Mega por R$ 119,90. Quer contratar?"},
                {"direction": "INBOUND", "body": original.body},
            ],
            "confirmed_facts": {"city": "Santa Rosa"},
        },
    )
    monkeypatch.setattr(
        "app.services.ai_seller.start_closing_from_sdr",
        lambda *_a, **kwargs: observed.append(kwargs["decision"]) or True,
    )
    assert process_ai_job(db, job) == "fast_handoff"
    assert observed[0].offer_plan_code == "Fibra 800 Mega"
    assert observed[0].offer_price == 119.90


def test_activation_requires_an_explicit_crm_confirmation():
    assert not has_confirmed_activation({
        "opportunity": {"status": "won"}, "contract": {"status": "assinado"},
    })
    assert not has_confirmed_activation({"activation": {"status": "agendada"}})
    assert has_confirmed_activation({"opportunity": {"activationStatus": "Ativada"}})


def test_direct_fiber_price_reuses_confirmed_city_segment_and_latest_correction():
    history = [
        {"id": 1, "direction": "INBOUND", "body": "Quero fibra residencial em Santa Rosa"},
        {"id": 2, "direction": "OUTBOUND", "body": "Qual velocidade você procura?"},
        {"id": 3, "direction": "INBOUND", "body": "Na verdade é em Crissiumal. Qual o valor de 800 Mega?", "created_at": "2026-11-20T18:30:00"},
    ]
    facts = _confirmed_facts_from_history(history)
    assert facts["city"] == "Crissiumal"
    assert facts["sources"]["city"] == {"message_id": 3, "at": "2026-11-20T18:30:00"}
    decision = run_ai_decision(None, {
        "latest_customer_message": history[-1]["body"], "history": history, "confirmed_facts": facts,
    })
    assert "800 Mega não está disponível em Crissiumal" in decision.reply_text
    assert "R$ 99,90" in decision.reply_text and "R$ 89,90" in decision.reply_text
    assert decision._timing["model_ms"] == 0


def test_company_quote_uses_confirmed_profile_and_asks_only_for_missing_city():
    raw = "Qual o valor da internet de 650 Mega para minha loja?"
    context = {"latest_customer_message": raw, "history": [], "lead": {"city": "Santa Rosa"}}
    decision = contextual_catalog_reply(context)
    assert decision.journey_state == "CONFIRM_CITY"
    assert decision.reply_text.count("?") == 1
    assert "R$" not in decision.reply_text

    city_context = {**context, "confirmed_facts": {"city": "Santo Ângelo"}}
    decision = contextual_catalog_reply(city_context)
    assert "fibra corporativa de 650 Mega" in decision.reply_text
    assert "R$ 199,90/mês" in decision.reply_text
    assert decision.qualification.customer_type == "EMPRESA"


def test_catalog_context_answers_product_details_before_contact_triage():
    facts = {"city": "Santa Rosa", "customer_type": "RESIDENCIAL", "product_kind": "MOBILE"}
    context = {"confirmed_facts": facts, "latest_customer_message": "O plano inclui WhatsApp?"}
    answer = SimpleNamespace(intent="INFORMATION", sales_state="DISCOVERY", offer_plan_code=None)
    assert _official_catalog_for_context(context)["segment"] == "MOVEL"
    assert _derive_journey_state(answer, context) == "SELECT_OFFER"

    context = {
        "confirmed_facts": {**facts, "product_kind": "COMBO"},
        "latest_customer_message": "Qual a velocidade de upload da fibra?",
    }
    catalog = _official_catalog_for_context(context)
    assert catalog["segment"] == "RESIDENCIAL" and catalog["city"] == "Santa Rosa"
    assert catalog["mobile"]["segment"] == "MOVEL"
    assert _derive_journey_state(answer, context) == "SELECT_OFFER"


def test_combo_and_fiber_only_show_distinct_terms_and_reuse_selected_products(monkeypatch):
    from app.services import ai_campaign_black as black

    monkeypatch.setattr(black, "black_active", lambda: True)
    context = {
        "latest_customer_message": "Quanto fica o combo 650 Mega com 5GB ou só fibra?",
        "confirmed_facts": {"city": "Santa Rosa", "customer_type": "RESIDENCIAL"},
    }
    quote = contextual_catalog_reply(context)
    assert "Fibra 650 Mega por R$ 99,90/mês" in quote.reply_text
    assert "Sulmóvel 5GB por R$ 44,90/mês" in quote.reply_text
    assert "Total de R$ 144,80/mês" in quote.reply_text
    assert f"dos dois vence em {black.FIRST_DUE_DATE}" in quote.reply_text
    assert "Só a fibra fica R$ 99,90/mês. A data da primeira fatura é confirmada no cadastro." in quote.reply_text

    history = [{"direction": "OUTBOUND", "body": quote.reply_text}]
    assert accepted_black_quote({**context, "history": history, "latest_customer_message": "Quero contratar"}) is None
    accepted = accepted_black_quote({**context, "history": history, "latest_customer_message": "Quero o combo"})
    assert accepted["plan"]["code"] == "FTTH_650"
    assert accepted["mobile_gb"] == 5
    solo = contextual_catalog_reply({**context, "history": history, "latest_customer_message": "Só fibra"})
    assert black.FIRST_DUE_DATE not in solo.reply_text
    assert "fibra residencial avulsa de 650 Mega" in solo.reply_text

    selected = [*history, {"direction": "INBOUND", "body": "Quero o combo"}]
    assert black.black_accepted(selected) and black.black_choice(selected) == "COMBO"
    selected_solo = [*selected, {"direction": "OUTBOUND", "body": solo.reply_text}, {"direction": "INBOUND", "body": "Quero contratar"}]
    assert not black.black_accepted(selected_solo) and not black.black_choice(selected_solo)


def test_answer_to_city_question_reuses_previously_requested_speed_and_franchise(monkeypatch):
    from app.services import ai_campaign_black as black

    monkeypatch.setattr(black, "black_active", lambda: True)
    history = [
        {"direction": "INBOUND", "body": "Quero combo de fibra 650 Mega e móvel 5GB para casa"},
        {"direction": "OUTBOUND", "body": "Em qual cidade seria a instalação? Assim consulto os planos disponíveis."},
        {"direction": "INBOUND", "body": "Santa Rosa"},
    ]
    quote = contextual_catalog_reply({
        "history": history, "confirmed_facts": _confirmed_facts_from_history(history),
        "latest_customer_message": "Santa Rosa",
    })
    assert "Fibra 650 Mega" in quote.reply_text
    assert "Sulmóvel 5GB" in quote.reply_text
    assert "Total de R$ 144,80/mês" in quote.reply_text


def test_mobile_budget_uses_benefit_and_checks_campaign_validity(monkeypatch):
    from app.services import ai_campaign_black as black

    monkeypatch.setattr(black, "black_active", lambda: True)
    quote = contextual_catalog_reply({
        "latest_customer_message": "Já sou cliente Sulnet. Qual plano móvel mais barato com WhatsApp?",
    })
    assert "Sulmóvel 5GB custa R$ 44,90/mês" in quote.reply_text
    assert "WhatsApp sem consumir a franquia" in quote.reply_text
    assert black.FIRST_DUE_DATE in quote.reply_text
    acceptance = {"history": [{"direction": "OUTBOUND", "body": quote.reply_text}], "latest_customer_message": "Quero contratar"}
    assert accepted_black_quote(acceptance)["plan"]["code"] == "SULMOVEL_5GB"
    monkeypatch.setattr(black, "black_active", lambda: False)
    assert accepted_black_quote(acceptance) is None
    refreshed = contextual_catalog_reply(acceptance)
    assert "Sulmóvel 5GB custa R$ 44,90/mês" in refreshed.reply_text
    assert black.FIRST_DUE_DATE not in refreshed.reply_text


@pytest.mark.parametrize(("text", "earlier", "issue"), [
    ("Queria aumentar", "Já tenho um plano", "Alteração do plano atual"),
    ("Queria mais velocidade", "Quero contratar fibra", ""),
    ("Minha internet da Vivo está lenta. Quero conhecer a fibra Sulnet.", "", ""),
    ("Parem de mandar mensagem. Minha internet tá lenta.", "", ""),
])
def test_central_recognizes_current_service_without_misrouting_prospects(text, earlier, issue):
    handoff = service_handoff({
        "latest_customer_message": text,
        "history": [{"direction": "INBOUND", "body": earlier}],
    })
    assert handoff.get("issue", "") == issue


def test_support_handoff_records_both_requests_and_prepares_central_message(chat):
    db, conversation, agent, queue = chat
    lead = Lead(business_name="Caso de teste", stage="NEGOCIACAO")
    db.add(lead)
    db.flush()
    conversation.lead_id = lead.id
    _inbound(db, conversation, "Sou cliente Sulnet. Meu plano é de 650 Mega e a conexão está caindo.")
    _inbound(db, conversation, "Já recebi visita técnica e reiniciei o roteador. TEXTO_FORA_DO_RESUMO.")
    original = _inbound(db, conversation, "O problema persiste. Quero reduzir o valor do plano e quero uma nova conexão.")
    job = AIJob(conversation_id=conversation.id, message_id=original.id, agent_id=agent.id, queue_id=queue.id)
    db.add(job)
    db.flush()

    assert process_ai_job(db, job) == "completed"
    db.commit()
    delivery = db.scalar(select(AIReplyDelivery))
    parsed = urlsplit(delivery.link_url)
    text = parse_qs(parsed.query)["text"][0]
    assert parsed.hostname == "wa.me" and parsed.path == "/555535208000"
    assert "650 Mega" in text and "Quedas na conexão" in text
    assert "Reiniciei o roteador" in text and "Já recebi uma visita técnica" in text
    assert "revisar o valor" in text and "TEXTO_FORA_DO_RESUMO" not in text
    memory = json.loads(lead.raw_data)["ai_service_handoff"]
    assert memory["pending_requests"] == ["SUPORTE_CENTRAL", "REVISAO_VALOR", "NOVA_CONEXAO"]
    assert memory["inbound_id"] == original.id
    assert lead.stage == "NEGOCIACAO"
    activity = db.scalar(select(Activity).where(Activity.kind == "ATENDIMENTO_CENTRAL"))
    assert activity.status == "PENDENTE"
    assert "nova conexão" in activity.description
    assert conversation.status != "RESOLVIDO"
    assert enqueue_ai_job(db, conversation=conversation, message=_inbound(db, conversation, "Vamos seguir com a nova conexão."))


def test_support_during_closing_preserves_existing_contract_step(chat, monkeypatch):
    db, conversation, agent, queue = chat
    agent.role_type = "CLOSER"
    agent.execution_mode = "AUTO"
    state = AIClosingState(
        conversation_id=conversation.id, agent_id=agent.id, step="CPF", external_id="test-closing-support",
    )
    db.add_all([state, AIConversationState(conversation_id=conversation.id, agent_id=agent.id, state="CLOSING")])
    _inbound(db, conversation, "Já sou cliente Sulnet. Meu plano é de 650 Mega.")
    original = _inbound(db, conversation, "Minha internet está lenta e já testei por cabo.")
    job = AIJob(conversation_id=conversation.id, message_id=original.id, agent_id=agent.id, queue_id=queue.id)
    db.add(job)
    db.flush()
    monkeypatch.setattr("app.services.ai_seller.seller_operational", lambda: True)
    assert process_ai_job(db, job) == "completed"
    assert state.step == "CPF"
    assert db.scalar(select(AIConversationState)).state == "CLOSING"
    db.flush()
    delivery = db.scalar(select(AIReplyDelivery))
    assert "Conexão lenta" in parse_qs(urlsplit(delivery.link_url).query)["text"][0]
