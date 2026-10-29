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
from app.services.ai_sdr import AIDecision, QualificationData, claim_ai_job, process_ai_job
from app.services.conversation_engine import store_received_payload
from app.services.queue_routing import queue_for_conversation
from app.services.seed import seed_database
from app.services.whatsapp_presence import join_whatsapp
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance, queue_instance


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
