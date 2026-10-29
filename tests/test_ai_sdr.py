from __future__ import annotations

from dataclasses import replace
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIQueueBinding,
    AIRun,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
    ZApiInstance,
)
from app.security import hash_password
from app.services.ai_sdr import (
    AIDecision,
    QualificationData,
    _apply_catalog_navigation_policy,
    _apply_offer_rejection_diagnostic_policy,
    _apply_secondary_decline_reengagement_policy,
    _apply_unavailable_plan_policy,
    _is_company_context,
    _sanitize_unconfirmed_customer_facts,
    build_ai_context,
    claim_ai_job,
    process_ai_job,
)
from app.services.conversation_engine import store_received_payload
from app.services.queue_routing import queue_for_conversation
from app.services.seed import seed_database
from app.services.whatsapp_presence import join_whatsapp
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance, queue_instance
from app.workers.ai_sdr_worker import _recover_recent_unqueued_inbounds


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _create_bound_digital_queue(db, suffix: str):
    agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital")
    )
    assert agent is not None

    queue = ServiceQueue(
        name=f"Fila Digital IA Teste {suffix}",
        code=f"DIGITAL-AI-{suffix}",
        team="DIGITAL",
        routing_strategy="ROUND_ROBIN",
        active=True,
    )
    db.add(queue)
    db.flush()
    db.add(
        AIQueueBinding(
            queue_id=queue.id,
            agent_id=agent.id,
            handling_mode="AI_FIRST",
            active=True,
            fallback_to_human=True,
        )
    )
    instance = create_zapi_instance(
        db,
        name=f"WhatsApp IA Teste {suffix}",
        phone_label=f"IA {suffix}",
        base_url="https://api.z-api.io",
        instance_id=f"instance-ai-{suffix.lower()}",
        instance_token=f"token-ai-{suffix.lower()}",
        client_token=f"client-ai-{suffix.lower()}",
        created_by_id=None,
    )
    bind_queue_instance(db, queue.id, instance.id)

    user = User(
        name="Consultor Digital Shadow",
        email=f"shadow-consultor-{suffix.lower()}@sulnet.local",
        password_hash=hash_password("SenhaShadow@2026"),
        role="CONSULTOR",
        team="DIGITAL",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    db.add(ServiceQueueMember(queue_id=queue.id, user_id=user.id, active=True))
    join_whatsapp(db, user)
    db.commit()
    return agent, queue, user


def test_seed_creates_sulia_user_and_profile():
    with TestClient(app):
        db = SessionLocal()
        try:
            ai_user = db.scalar(
                select(User).where(User.email == "sulia.sdr@system.local")
            )
            assert ai_user is not None
            assert ai_user.role == "AI_SDR"
            agent = db.scalar(
                select(AIAgentProfile).where(AIAgentProfile.user_id == ai_user.id)
            )
            assert agent is not None
            assert agent.execution_mode == "SHADOW"
            assert agent.model == "gpt-6-luna"
        finally:
            db.close()


def test_ai_dashboard_is_available_to_admin():
    with TestClient(app) as client:
        _login_admin(client)
        page = client.get("/ai")
        assert page.status_code == 200
        assert "SulIA SDR" in page.text
        assert "Shadow Mode" in page.text
        assert "Base de Conhecimento" in page.text


def test_inbound_message_creates_shadow_job_and_keeps_human_routing():
    with TestClient(app):
        db = SessionLocal()
        try:
            _agent, queue, human = _create_bound_digital_queue(db, "ENQUEUE")
            from app.models import LeadQueuePreference, Lead
            lead = Lead(
                business_name="Cliente IA Shadow",
                contact_name="Rafael",
                phone="5555988877111",
                segment="Empresa",
                city="Santa Rosa",
                source="MANUAL",
                origin_channel="WHATSAPP",
                team="DIGITAL",
                stage="NOVO",
                score=0,
                temperature="FRIO",
                priority="NORMAL",
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="TEST_AI",
                )
            )
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-SHADOW-INBOUND-1",
                    "senderName": "Rafael",
                    "text": {"message": "Tenho interesse, mas hoje minha internet cai bastante."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()

            assert created is True
            assert conversation is not None
            assert message is not None
            assert conversation.assigned_user_id == human.id

            job = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
            assert job is not None
            assert job.queue_id == queue.id
            assert job.status == "PENDENTE"
            job.status = "IGNORADO"
            db.commit()
        finally:
            db.close()


def test_shadow_job_stores_decision_without_sending(monkeypatch):
    decision = AIDecision(
        reply_text="Entendi. Hoje vocês usam a internet principalmente para quais atividades?",
        intent="INTEREST",
        qualification_score=62,
        temperature="MORNO",
        recommended_stage="QUALIFICADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.91,
        summary="Cliente demonstra interesse e relata instabilidade na conexão atual.",
        next_action="Entender uso principal e ambiente.",
        qualification=QualificationData(
            pain_points=["instabilidade"],
            decision_maker="NAO_INFORMADO",
            city="Santa Rosa",
        ),
        crm_notes=["Dor identificada: quedas frequentes."],
    )

    monkeypatch.setattr(
        "app.services.ai_sdr.run_ai_decision",
        lambda agent, context: decision,
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            agent, queue, _human = _create_bound_digital_queue(db, "PROCESS")
            from app.models import Lead, LeadQueuePreference
            lead = Lead(
                business_name="Cliente Shadow Execução",
                contact_name="Cliente",
                phone="5555988877222",
                segment="Empresa",
                city="Santa Rosa",
                source="MANUAL",
                origin_channel="WHATSAPP",
                team="DIGITAL",
                stage="NOVO",
                score=0,
                temperature="FRIO",
                priority="NORMAL",
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="TEST_AI_RUN",
                )
            )
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-SHADOW-INBOUND-2",
                    "senderName": "Cliente",
                    "text": {"message": "Quero conhecer melhor, minha internet vive caindo."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True

            outbound_before = len(
                list(
                    db.scalars(
                        select(Message).where(
                            Message.conversation_id == conversation.id,
                            Message.direction == "OUTBOUND",
                        )
                    )
                )
            )

            job = claim_ai_job(db)
            assert job is not None
            db.commit()
            job = db.get(AIJob, job.id)
            outcome = process_ai_job(db, job)
            db.commit()

            assert outcome == "completed"
            run = db.scalar(select(AIRun).where(AIRun.job_id == job.id))
            assert run is not None
            assert run.execution_mode == "SHADOW"
            assert run.reply_text.startswith("Entendi.")
            assert run.qualification_score == 62
            assert run.intent == "INTEREST"

            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id
                )
            )
            assert state is not None
            assert state.state == "SHADOW_REVIEW"
            assert state.qualification_score == 62

            outbound_after = len(
                list(
                    db.scalars(
                        select(Message).where(
                            Message.conversation_id == conversation.id,
                            Message.direction == "OUTBOUND",
                        )
                    )
                )
            )
            assert outbound_after == outbound_before
            assert lead.stage == "NOVO"
            assert lead.score == 0
        finally:
            db.close()



def test_auto_mode_sends_reply_to_whatsapp(monkeypatch):
    decision = AIDecision(
        reply_text="Claro! Qual é o seu provedor atual?",
        intent="INTEREST",
        qualification_score=55,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.94,
        summary="Cliente demonstrou interesse.",
        next_action="Entender o provedor atual.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        followup_needed=False,
    )
    monkeypatch.setattr("app.services.ai_sdr.settings", replace(settings, ai_autonomous_enabled=True))
    monkeypatch.setattr(
        "app.services.ai_sdr.run_ai_decision",
        lambda agent, context: decision,
    )
    sent = []
    monkeypatch.setattr(
        "app.services.zapi.ZApiService.send_text",
        lambda self, *, phone, message: (
            sent.append((phone, message))
            or {"messageId": "AI-AUTO-OUT-1", "zaapId": "AI-AUTO-ZAAP-1"}
        ),
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            agent, queue, _human = _create_bound_digital_queue(db, "AUTO")
            agent.execution_mode = "AUTO"
            db.commit()
            from app.models import Lead, LeadQueuePreference
            lead = Lead(
                business_name="Cliente IA Automática",
                contact_name="Cliente",
                phone="5555988877333",
                segment="Empresa",
                city="Santa Rosa",
                source="MANUAL",
                origin_channel="WHATSAPP",
                team="DIGITAL",
                stage="NOVO",
                score=0,
                temperature="FRIO",
                priority="NORMAL",
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="TEST_AI_AUTO",
                )
            )
            db.commit()

            conversation, _message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-AUTO-INBOUND-1",
                    "senderName": "Cliente",
                    "text": {"message": "Tenho interesse em conhecer os planos."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True

            job = claim_ai_job(db)
            assert job is not None
            db.commit()
            job = db.get(AIJob, job.id)
            outcome = process_ai_job(db, job)
            db.commit()

            assert outcome == "completed"
            assert sent == [(lead.phone, decision.reply_text)]
            run = db.scalar(select(AIRun).where(AIRun.job_id == job.id))
            assert run is not None
            assert run.execution_mode == "AUTO"
            outbound = db.scalar(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.provider_message_id == "AI-AUTO-OUT-1",
                )
            )
            assert outbound is not None
            assert outbound.direction == "OUTBOUND"
            assert outbound.sender_user_id == agent.user_id
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation.id
                )
            )
            assert state is not None
            assert state.state == "AI_ACTIVE"

            followup = db.scalar(
                select(AIFollowupJob).where(
                    AIFollowupJob.conversation_id == conversation.id,
                    AIFollowupJob.status == "PENDENTE",
                )
            )
            assert followup is not None
            assert followup.step == 1
        finally:
            db.close()

def test_seed_binds_sulia_to_all_active_digital_queues():
    with TestClient(app):
        db = SessionLocal()
        try:
            suffix = "SEED-ALL-DIGITAL"
            queues = []
            for index in (1, 2):
                queue = ServiceQueue(
                    name=f"Fila Digital Seed {index}",
                    code=f"DIGITAL-SEED-{index}",
                    team="DIGITAL",
                    routing_strategy="ROUND_ROBIN",
                    active=True,
                )
                db.add(queue)
                db.flush()
                queues.append(queue)
            db.commit()

            seed_database(db, include_demo=False)
            for queue in queues:
                binding = db.scalar(
                    select(AIQueueBinding).where(
                        AIQueueBinding.queue_id == queue.id
                    )
                )
                assert binding is not None
                assert binding.active is True
                assert binding.handling_mode == "AI_FIRST"
        finally:
            db.close()


def test_seed_materializes_fila_ia_instance_as_service_queue():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = db.scalar(
                select(User).where(User.email == "admin@sulnet.local")
            )
            assert admin is not None
            instance = db.scalar(
                select(ZApiInstance).where(ZApiInstance.name == "Fila IA")
            )
            if not instance:
                instance = create_zapi_instance(
                    db,
                    name="Fila IA",
                    phone_label="Número IA",
                    base_url="https://api.z-api.io",
                    instance_id="instance-fila-ia-seed-test",
                    instance_token="token-fila-ia-seed-test",
                    client_token="client-fila-ia-seed-test",
                    created_by_id=admin.id,
                )
                db.commit()

            seed_database(db, include_demo=False)

            queue = db.scalar(
                select(ServiceQueue).where(ServiceQueue.code == "FILA-IA")
            )
            assert queue is not None
            assert queue.name == "Fila IA"
            assert queue.team == "DIGITAL"
            assert queue.routing_strategy == "MANUAL"
            assert queue.active is True

            linked_instance = queue_instance(db, queue.id)
            assert linked_instance is not None
            assert linked_instance.id == instance.id

            binding = db.scalar(
                select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id)
            )
            assert binding is not None
            assert binding.active is True
            assert binding.handling_mode == "AI_FIRST"
        finally:
            db.close()


def test_ai_dashboard_shows_queue_whatsapp_instance():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            admin = db.scalar(
                select(User).where(User.email == "admin@sulnet.local")
            )
            assert admin is not None
            instance = db.scalar(
                select(ZApiInstance).where(ZApiInstance.name == "Fila IA")
            )
            if not instance:
                create_zapi_instance(
                    db,
                    name="Fila IA",
                    phone_label="Número IA",
                    base_url="https://api.z-api.io",
                    instance_id="instance-fila-ia-dashboard-test",
                    instance_token="token-fila-ia-dashboard-test",
                    client_token="client-fila-ia-dashboard-test",
                    created_by_id=admin.id,
                )
                db.commit()
            seed_database(db, include_demo=False)
        finally:
            db.close()

        _login_admin(client)
        page = client.get("/ai")
        assert page.status_code == 200
        assert "Fila IA" in page.text
        assert "WhatsApp: Fila IA" in page.text



def test_auto_mode_uses_buttons_for_casa_or_empresa_question(monkeypatch):
    decision = AIDecision(
        reply_text="Boa tarde! Você está buscando internet para sua casa ou empresa?",
        intent="QUALIFYING",
        qualification_score=20,
        temperature="FRIO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.98,
        summary="Início da qualificação do cliente.",
        next_action="Identificar se o uso é residencial ou empresarial.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
    )
    monkeypatch.setattr(
        "app.services.ai_sdr.settings",
        replace(settings, ai_autonomous_enabled=True),
    )
    monkeypatch.setattr(
        "app.services.ai_sdr.run_ai_decision",
        lambda agent, context: decision,
    )

    sent_buttons = []

    def fake_send_button_list(self, *, phone, message, buttons):
        sent_buttons.append((phone, message, buttons))
        return {
            "messageId": "AI-BUTTONS-OUT-1",
            "zaapId": "AI-BUTTONS-ZAAP-1",
        }

    def fail_send_text(self, *, phone, message):
        raise AssertionError("Pergunta casa/empresa deve ser enviada com botões.")

    monkeypatch.setattr(
        "app.services.zapi.ZApiService.send_button_list",
        fake_send_button_list,
    )
    monkeypatch.setattr(
        "app.services.zapi.ZApiService.send_text",
        fail_send_text,
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(
                select(AIJob).where(AIJob.status == "PENDENTE")
            ):
                stale_job.status = "IGNORADO"
            db.flush()

            agent, queue, _human = _create_bound_digital_queue(db, "BUTTONS")
            agent.execution_mode = "AUTO"
            db.commit()

            from app.models import Lead, LeadQueuePreference

            lead = Lead(
                business_name="Cliente Botões IA",
                contact_name="Cliente",
                phone="5555988877444",
                segment="Residencial",
                city="Santa Rosa",
                source="MANUAL",
                origin_channel="WHATSAPP",
                team="DIGITAL",
                stage="NOVO",
                score=0,
                temperature="FRIO",
                priority="NORMAL",
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="TEST_AI_BUTTONS",
                )
            )
            db.commit()

            conversation, _message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-BUTTONS-INBOUND-1",
                    "senderName": "Cliente",
                    "text": {"message": "Oi, queria informações."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True

            job = claim_ai_job(db)
            assert job is not None
            db.commit()
            job = db.get(AIJob, job.id)

            outcome = process_ai_job(db, job)
            db.commit()

            assert outcome == "completed"
            assert sent_buttons == [
                (
                    lead.phone,
                    decision.reply_text,
                    [
                        {"id": "para_casa", "label": "Para casa"},
                        {"id": "para_empresa", "label": "Para empresa"},
                    ],
                )
            ]
            outbound = db.scalar(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.provider_message_id == "AI-BUTTONS-OUT-1",
                )
            )
            assert outbound is not None
            assert outbound.message_type == "BUTTONS"
        finally:
            db.close()



def test_inbound_instance_rebinds_stale_conversation_to_ai_queue():
    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()

            _agent, ai_queue, _human = _create_bound_digital_queue(db, "REBOUND")
            instance = queue_instance(db, ai_queue.id)
            assert instance is not None

            old_queue = ServiceQueue(
                name="Fila Antiga Sem IA",
                code="OLD-NO-AI-REBOUND",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(old_queue)
            db.commit()

            conversation, first_message, created = store_received_payload(
                db,
                {
                    "phone": "5555988866333",
                    "messageId": "AI-REBOUND-IN-1",
                    "senderName": "Cliente Rebound",
                    "text": {"message": "Oi"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
                queue_id=old_queue.id,
                zapi_instance_id=instance.id,
            )
            db.commit()
            assert created is True
            assert queue_for_conversation(db, conversation.id).id == old_queue.id
            assert db.scalar(select(AIJob).where(AIJob.message_id == first_message.id)) is None

            same_conversation, second_message, created = store_received_payload(
                db,
                {
                    "phone": "5555988866333",
                    "messageId": "AI-REBOUND-IN-2",
                    "senderName": "Cliente Rebound",
                    "text": {"message": "Quero saber os planos"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
                queue_id=ai_queue.id,
                zapi_instance_id=instance.id,
            )
            db.commit()

            assert created is True
            assert same_conversation.id == conversation.id
            assert queue_for_conversation(db, conversation.id).id == ai_queue.id
            job = db.scalar(select(AIJob).where(AIJob.message_id == second_message.id))
            assert job is not None
            assert job.queue_id == ai_queue.id
            assert job.status == "PENDENTE"
        finally:
            db.close()



def test_worker_recovers_recent_inbound_that_missed_ai_job():
    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()

            _agent, ai_queue, _human = _create_bound_digital_queue(db, "RECOVER-MISSED")
            instance = queue_instance(db, ai_queue.id)
            assert instance is not None

            old_queue = ServiceQueue(
                name="Fila Antiga Recovery",
                code="OLD-RECOVERY",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(old_queue)
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": "5555988866444",
                    "messageId": "AI-MISSED-IN-1",
                    "senderName": "Cliente Recovery",
                    "text": {"message": "Quero internet"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
                queue_id=old_queue.id,
                zapi_instance_id=instance.id,
            )
            db.commit()
            assert created is True
            assert db.scalar(select(AIJob).where(AIJob.message_id == message.id)) is None
            assert queue_for_conversation(db, conversation.id).id == old_queue.id
        finally:
            db.close()

        recovered = _recover_recent_unqueued_inbounds()
        assert recovered >= 1

        db = SessionLocal()
        try:
            job = db.scalar(
                select(AIJob).where(
                    AIJob.message_id == message.id,
                    AIJob.status == "PENDENTE",
                )
            )
            assert job is not None
            assert job.queue_id == ai_queue.id
            assert queue_for_conversation(db, conversation.id).id == ai_queue.id
        finally:
            db.close()



def test_secondary_decline_returns_to_main_offer():
    decision = AIDecision(
        reply_text=(
            "Tudo bem! Não vou verificar como funciona o acesso. "
            "Se quiser saber mais sobre o Sulnet Play, é só me chamar."
        ),
        intent="INFORMATION",
        qualification_score=65,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.95,
        summary="Cliente recusou apenas informação adicional do Sulnet Play.",
        next_action="Aguardar cliente.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="WAITING_CUSTOMER",
    )
    context = {
        "latest_customer_message": "Não, obrigado",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "type": "TEXT",
                "body": (
                    "Legal! Em Santa Rosa temos o plano de fibra óptica de "
                    "800 Mega por R$ 119,90 mensais."
                ),
            },
            {
                "direction": "INBOUND",
                "type": "TEXT",
                "body": "O que tem no Sulnet Play?",
            },
            {
                "direction": "OUTBOUND",
                "type": "BUTTONS",
                "body": (
                    "Não tenho detalhes aprovados sobre o conteúdo do Sulnet Play. "
                    "Quer que eu também confira como funciona o acesso?"
                ),
            },
            {
                "direction": "INBOUND",
                "type": "TEXT",
                "body": "Não, obrigado",
            },
        ],
    }

    result = _apply_secondary_decline_reengagement_policy(decision, context)

    assert result.reply_text == (
        "Sem problema! Podemos dar andamento à contratação do plano de fibra óptica "
        "de 800 Mega por R$ 119,90 mensais?"
    )
    assert [item.label for item in result.quick_replies] == [
        "Quero contratar",
        "Tenho outra dúvida",
    ]
    assert result.intent == "INTEREST"
    assert result.sales_state == "OFFERING"
    assert result.handoff_recommended is False


def test_decline_to_direct_contract_question_is_not_reinterpreted():
    decision = AIDecision(
        reply_text="Tudo bem. Posso ajudar com outra informação.",
        intent="NOT_INTERESTED",
        qualification_score=40,
        temperature="FRIO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente recusou a contratação.",
        next_action="Tratar objeção ou programar follow-up.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="WAITING_CUSTOMER",
    )
    context = {
        "latest_customer_message": "Não, obrigado",
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Quer aproveitar e contratar esse plano agora?",
            },
            {
                "direction": "INBOUND",
                "body": "Não, obrigado",
            },
        ],
    }

    result = _apply_secondary_decline_reengagement_policy(decision, context)
    assert result.reply_text == "Tudo bem. Posso ajudar com outra informação."
    assert result.intent == "NOT_INTERESTED"



def _base_rejection_decision():
    return AIDecision(
        reply_text="Entendi, obrigado por avisar. Não vou insistir na oferta de internet.",
        intent="NOT_INTERESTED",
        qualification_score=35,
        temperature="FRIO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.95,
        summary="Cliente rejeitou a oferta de internet.",
        next_action="Encerrar conversa.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        objection="NO_INTEREST",
        sales_state="WAITING_CUSTOMER",
    )


def test_offer_rejection_opens_residential_diagnostic_instead_of_goodbye():
    decision = _base_rejection_decision()
    context = {
        "latest_customer_message": "eu não quero nada de internet",
        "lead": {
            "segment": "Residencial",
            "business_name": "",
            "city": "Santa Rosa",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Claro! Qual é a sua dúvida?",
            },
            {
                "direction": "INBOUND",
                "body": "eu não quero nada de internet",
            },
        ],
    }

    result = _apply_offer_rejection_diagnostic_policy(decision, context)

    assert result.reply_text == (
        "Entendi. Hoje você já possui conexão de internet?"
    )
    assert [item.label for item in result.quick_replies] == ["Sim", "Não"]
    assert result.intent == "QUALIFYING"
    assert result.sales_state == "DISCOVERY"
    assert result.handoff_recommended is False
    assert result.followup_needed is True


def test_offer_rejection_uses_company_diagnostic_for_business_lead():
    decision = _base_rejection_decision()
    context = {
        "latest_customer_message": "não tenho interesse em internet",
        "lead": {
            "segment": "Empresa",
            "business_name": "Mercado Teste",
            "city": "Santa Rosa",
        },
        "history": [
            {
                "direction": "INBOUND",
                "body": "Estou vendo isso para minha empresa.",
            },
            {
                "direction": "OUTBOUND",
                "body": "Podemos avançar com o plano de 800 Mega?",
            },
            {
                "direction": "INBOUND",
                "body": "não tenho interesse em internet",
            },
        ],
    }

    result = _apply_offer_rejection_diagnostic_policy(decision, context)

    assert result.reply_text == (
        "Entendi. Hoje sua empresa já possui conexão de internet?"
    )
    assert [item.label for item in result.quick_replies] == ["Sim", "Não"]


def test_explicit_opt_out_is_not_overridden_by_sales_reengagement():
    decision = _base_rejection_decision()
    decision.intent = "DO_NOT_CONTACT"
    decision.sales_state = "OPT_OUT"
    context = {
        "latest_customer_message": "não quero mais mensagens, não me contate",
        "lead": {
            "segment": "Residencial",
            "business_name": "",
        },
        "history": [],
    }

    result = _apply_offer_rejection_diagnostic_policy(decision, context)

    assert result.intent == "DO_NOT_CONTACT"
    assert result.sales_state == "OPT_OUT"
    assert result.reply_text == (
        "Entendi, obrigado por avisar. Não vou insistir na oferta de internet."
    )


def test_offer_rejection_diagnostic_is_not_repeated_indefinitely():
    decision = _base_rejection_decision()
    context = {
        "latest_customer_message": "não quero internet",
        "lead": {
            "segment": "Residencial",
            "business_name": "",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Entendi. Hoje você já possui conexão de internet na sua casa?",
            },
            {
                "direction": "INBOUND",
                "body": "não quero internet",
            },
        ],
    }

    result = _apply_offer_rejection_diagnostic_policy(decision, context)

    assert result.reply_text == (
        "Entendi, obrigado por avisar. Não vou insistir na oferta de internet."
    )



def test_company_context_ignores_ai_outbound_claims():
    context = {
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Para uma empresa que usa internet para emitir notas fiscais, "
                    "contar com uma empresa regional pode ser um diferencial."
                ),
            },
            {
                "direction": "INBOUND",
                "body": "?",
            },
        ]
    }

    assert _is_company_context(context) is False


def test_unconfirmed_provider_is_removed_before_reuse():
    decision = AIDecision(
        reply_text=(
            "Entendi — vocês estão satisfeitos com a conexão atual da Brphoni?"
        ),
        intent="QUALIFYING",
        qualification_score=45,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.8,
        summary="Cliente usa Brphoni atualmente.",
        next_action="Comparar a Brphoni com a Sulnet.",
        qualification=QualificationData(
            city="Santa Rosa",
            current_provider="Brphoni",
        ),
        crm_notes=[],
        sales_state="DISCOVERY",
        proposal_summary="Comparar Brphoni com a Sulnet.",
    )
    context = {
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Você usa Brphoni hoje?",
            },
            {
                "direction": "INBOUND",
                "body": "eu não quero nada de internet",
            },
            {
                "direction": "INBOUND",
                "body": "?",
            },
        ]
    }

    result = _sanitize_unconfirmed_customer_facts(decision, context)

    assert result.qualification.current_provider is None
    assert "Brphoni" not in result.reply_text
    assert "Brphoni" not in result.summary
    assert "Brphoni" not in result.next_action
    assert "Brphoni" not in result.proposal_summary


def test_ai_context_does_not_feed_cross_conversation_ai_memory_back_as_fact():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent, queue, _human = _create_bound_digital_queue(db, "SOURCE-ISOLATION")
            from app.models import Lead, LeadQueuePreference

            lead = Lead(
                business_name="Empresa Antiga do CRM",
                contact_name="Cliente",
                phone="5555988877999",
                segment="Empresa",
                city="Santa Rosa",
                source="MANUAL",
                origin_channel="WHATSAPP",
                team="DIGITAL",
                stage="CONTATO_REALIZADO",
                score=25,
                temperature="FRIO",
                priority="NORMAL",
                notes="[IA SDR] Cliente usa Brphoni.",
                potential="[IA SDR] Memória comercial: provedor=Brphoni; uso=notas fiscais",
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="TEST_SOURCE_ISOLATION",
                )
            )
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-SOURCE-ISOLATION-IN-1",
                    "senderName": "Cliente",
                    "text": {"message": "?"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True

            job = db.scalar(
                select(AIJob).where(AIJob.message_id == message.id)
            )
            assert job is not None

            context = build_ai_context(db, job=job, agent=agent)

            assert "notes" not in context["lead"]
            assert "potential" not in context["lead"]
            assert "segment" not in context["lead"]
            assert "business_name" not in context["lead"]
            assert "summary" not in context["previous_ai_state"]
            assert context["customer_evidence"] == [
                {
                    "body": "?",
                    "created_at": message.created_at.isoformat(),
                }
            ]
            assert "Brphoni" not in str(context)
            assert "notas fiscais" not in str(context)
        finally:
            db.close()


def _base_unavailable_plan_decision():
    return AIDecision(
        reply_text="Vou transferir você para um vendedor verificar essa condição.",
        intent="HUMAN_REQUEST",
        qualification_score=70,
        temperature="QUENTE",
        recommended_stage="NEGOCIACAO",
        handoff_recommended=True,
        handoff_reason="Plano solicitado não encontrado.",
        confidence=0.9,
        summary="Cliente pediu plano fora da base.",
        next_action="Transferir ao vendedor.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="READY_FOR_HUMAN_SALES",
        customer_commitment=False,
    )


def test_unavailable_plan_in_known_city_stays_with_sdr_and_offers_valid_options():
    decision = _base_unavailable_plan_decision()
    context = {
        "latest_customer_message": "Vocês têm plano de 500 Mega?",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {"direction": "INBOUND", "body": "Sou de Santa Rosa."},
            {"direction": "INBOUND", "body": "Vocês têm plano de 500 Mega?"},
        ],
    }

    result = _apply_unavailable_plan_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.handoff_reason == ""
    assert result.sales_state == "OFFERING"
    assert result.intent == "INTEREST"
    assert result.customer_commitment is False
    assert result.offer_plan_code is None
    assert "500 Mega não está disponível em Santa Rosa" in result.reply_text
    assert "800 Mega por R$ 119,90" in result.reply_text
    assert "650 Mega por R$ 99,90" in result.reply_text
    assert [item.label for item in result.quick_replies] == ["800 Mega", "650 Mega"]


def test_unavailable_plan_without_city_asks_city_instead_of_handoff():
    decision = _base_unavailable_plan_decision()
    decision.qualification.city = None
    context = {
        "latest_customer_message": "Quero um plano de 400 Mega",
        "lead": {"city": ""},
        "history": [
            {"direction": "INBOUND", "body": "Quero um plano de 400 Mega"},
        ],
    }

    result = _apply_unavailable_plan_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "DISCOVERY"
    assert result.intent == "QUALIFYING"
    assert result.customer_commitment is False
    assert "Qual é a sua cidade?" in result.reply_text


def test_requested_plan_that_exists_in_city_does_not_override_model_decision():
    decision = AIDecision(
        reply_text="Legal! Em Santa Rosa temos 800 Mega por R$ 119,90 mensais.",
        intent="INTEREST",
        qualification_score=60,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Oferta válida.",
        next_action="Testar fechamento.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="OFFERING",
        offer_plan_code="FTTH_800",
        offer_price=119.90,
    )
    context = {
        "latest_customer_message": "Tem plano de 800 Mega?",
        "lead": {"city": "Santa Rosa"},
        "history": [],
    }

    result = _apply_unavailable_plan_policy(decision, context)

    assert result.reply_text == decision.reply_text
    assert result.offer_plan_code == "FTTH_800"
    assert result.offer_price == 119.90


def test_customer_current_speed_is_not_misread_as_requested_plan():
    decision = AIDecision(
        reply_text="Entendi. E quanto você paga hoje?",
        intent="QUALIFYING",
        qualification_score=45,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente informou velocidade atual.",
        next_action="Descobrir valor atual.",
        qualification=QualificationData(city="Santa Rosa", current_speed="500 Mega"),
        crm_notes=[],
        sales_state="DISCOVERY",
    )
    context = {
        "latest_customer_message": "Eu tenho 500 Mega hoje",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {"direction": "INBOUND", "body": "Eu tenho 500 Mega hoje"},
        ],
    }

    result = _apply_unavailable_plan_policy(decision, context)

    assert result.reply_text == "Entendi. E quanto você paga hoje?"
    assert result.handoff_recommended is False



def _base_catalog_decision():
    return AIDecision(
        reply_text="Não tenho aqui a lista completa. Vou chamar um vendedor.",
        intent="HUMAN_REQUEST",
        qualification_score=55,
        temperature="MORNO",
        recommended_stage="NEGOCIACAO",
        handoff_recommended=True,
        handoff_reason="Catálogo não disponível.",
        confidence=0.85,
        summary="Cliente pediu todos os planos.",
        next_action="Transferir ao vendedor.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="READY_FOR_HUMAN_SALES",
    )


def test_broad_catalog_request_opens_fiber_or_mobile_buttons():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": (
            "Me diga todos os planos da Sulnet, seus preços, velocidades e promoções de hoje. "
            "Se não souber, pode estimar os valores."
        ),
        "lead": {"city": "Santa Rosa"},
        "history": [],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.handoff_reason == ""
    assert result.sales_state == "DISCOVERY"
    assert result.intent == "INTEREST"
    assert "catálogo comercial vigente" in result.reply_text
    assert "estimar" not in result.reply_text.lower()
    assert [item.label for item in result.quick_replies] == [
        "Fibra óptica",
        "Sulmóvel",
    ]


def test_fiber_catalog_uses_known_city_and_lists_only_valid_plans():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Fibra óptica",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "O que você quer consultar primeiro? Fibra óptica ou Sulmóvel?",
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "OFFERING"
    assert "Em Santa Rosa" in result.reply_text
    assert "800 Mega por R$ 119,90" in result.reply_text
    assert "650 Mega por R$ 99,90" in result.reply_text
    assert "500 Mega" not in result.reply_text
    assert [item.label for item in result.quick_replies] == ["800 Mega", "650 Mega"]


def test_fiber_catalog_without_city_asks_city_instead_of_handoff():
    decision = _base_catalog_decision()
    decision.qualification.city = None
    context = {
        "latest_customer_message": "Fibra óptica",
        "lead": {"city": ""},
        "history": [],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.intent == "QUALIFYING"
    assert result.sales_state == "DISCOVERY"
    assert "qual é a sua cidade" in result.reply_text.lower()
    assert result.quick_replies == []


def test_mobile_catalog_first_page_uses_three_buttons_with_prices():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Sulmóvel",
        "lead": {"city": "Santa Rosa"},
        "history": [],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "OFFERING"
    assert [item.label for item in result.quick_replies] == [
        "5GB - R$ 44,90",
        "7GB - R$ 47,90",
        "Mais planos",
    ]


def test_mobile_catalog_more_plans_paginates_remaining_options():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Mais planos",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "No Sulmóvel temos planos nacionais com valores confirmados. "
                    "Escolha: 5GB ou 7GB."
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert [item.label for item in result.quick_replies] == [
        "10GB - R$ 53,90",
        "15GB - R$ 69,80",
        "Voltar",
    ]


def test_mobile_catalog_back_returns_to_first_mobile_page():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Voltar",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Mais opções do Sulmóvel: 10GB, 15GB ou Voltar.",
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert [item.label for item in result.quick_replies] == [
        "5GB - R$ 44,90",
        "7GB - R$ 47,90",
        "Mais planos",
    ]
