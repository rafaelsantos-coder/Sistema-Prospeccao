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
    Lead,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
    ZApiInstance,
)
from app.security import hash_password
from app.services.ai_followups import _followup_exposes_internal_handoff, _followup_is_repetitive, _pending_action_from_theme, _unique_followup_fallback
from app.services.ai_sdr_policy import followup_spec
from app.services.ai_sdr import (
    AIDecision,
    QualificationData,
    _apply_catalog_navigation_policy,
    _apply_corporate_customer_policy,
    _apply_existing_customer_plan_change_policy,
    _apply_journey_orchestration_policy,
    _apply_offer_rejection_diagnostic_policy,
    _apply_prospecting_entry_policy,
    _apply_router_information_policy,
    _apply_secondary_decline_reengagement_policy,
    _apply_unavailable_plan_policy,
    _confirmed_customer_type_from_lead,
    _confirmed_existing_customer_from_lead,
    _confirmed_facts_from_history,
    _explicit_purchase_commitment,
    _is_company_context,
    _journey_integrity_issues,
    _journey_memory_from_lead,
    _persist_confirmed_customer_type,
    _persist_confirmed_existing_customer,
    _persist_journey_memory,
    _show_corporate_differentials,
    _sanitize_unconfirmed_customer_facts,
    _sdr_exposes_internal_handoff,
    _sdr_reply_repeats_recent_outbound,
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

def test_pausar_ia_na_fila_para_respostas_e_followups_do_sdr(monkeypatch):
    from app.models import LeadQueuePreference
    from app.services.ai_followups import _followup_allowed, schedule_followup

    monkeypatch.setattr("app.services.ai_sdr.settings", replace(settings, ai_autonomous_enabled=True))
    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            agent, queue, _human = _create_bound_digital_queue(db, "PAUSA")
            agent.execution_mode = "AUTO"
            db.commit()
            lead = Lead(
                business_name="Cliente Fila Pausada",
                contact_name="Cliente",
                phone="5555988877566",
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST_AI_PAUSE"))
            db.commit()
            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-PAUSE-INBOUND-1",
                    "senderName": "Cliente",
                    "text": {"message": "Quero saber dos planos."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True
            job = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
            assert job is not None

            state = db.scalar(
                select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
            )
            if state is None:
                state = AIConversationState(conversation_id=conversation.id, agent_id=agent.id, state="AI_ACTIVE")
                db.add(state)
            state.state = "AI_ACTIVE"
            state.agent_id = agent.id
            db.flush()
            followup = schedule_followup(
                db,
                agent_id=agent.id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                source_message_id=message.id,
            )
            assert followup is not None
            assert _followup_allowed(db, followup) == (True, "")

            # "Pausar IA" na fila.
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id))
            binding.active = False
            db.flush()

            assert _followup_allowed(db, followup) == (False, "FILA_SEM_IA")
            # O backfill não recria a cadência enquanto a fila estiver pausada.
            assert (
                schedule_followup(
                    db,
                    agent_id=agent.id,
                    queue_id=queue.id,
                    conversation_id=conversation.id,
                    source_message_id=message.id,
                )
                is None
            )
            calls = []
            monkeypatch.setattr(
                "app.services.ai_sdr.run_ai_decision",
                lambda agent, context: calls.append(context),
            )
            assert process_ai_job(db, job) == "skipped"
            assert job.status == "IGNORADO"
            assert calls == []

            # "Ativar IA" de novo.
            binding.active = True
            db.flush()
            assert _followup_allowed(db, followup) == (True, "")
        finally:
            db.rollback()
            db.close()


def test_vendedor_ia_nao_insiste_com_quem_desistiu_nem_com_fila_pausada(monkeypatch):
    from app.models import LeadQueuePreference
    from app.services.ai_followups import _followup_allowed, schedule_followup

    monkeypatch.setattr("app.services.ai_sdr.settings", replace(settings, ai_autonomous_enabled=True))
    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            _sdr, queue, _human = _create_bound_digital_queue(db, "CLOSER-PAUSA")
            closer = db.scalar(
                select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-vendedor-fechamento")
            )
            assert closer is not None
            assert closer.role_type == "CLOSER"
            lead = Lead(
                business_name="Cliente Vendedor Desistiu",
                contact_name="Cliente",
                phone="5555988877577",
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST_AI_CLOSER_PAUSE"))
            db.commit()
            conversation, message, _created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-CLOSER-PAUSE-1",
                    "senderName": "Cliente",
                    "text": {"message": "Quero contratar o plano de 650 Mega."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            state = db.scalar(
                select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
            )
            if state is None:
                state = AIConversationState(conversation_id=conversation.id, agent_id=closer.id, state="CLOSING")
                db.add(state)
            state.state = "CLOSING"
            state.agent_id = closer.id
            db.flush()

            followup = schedule_followup(
                db,
                agent_id=closer.id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                source_message_id=message.id,
            )
            assert followup is not None
            assert _followup_allowed(db, followup) == (True, "")

            # Fila pausada: o Vendedor IA também não puxa conversa.
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id))
            binding.active = False
            db.flush()
            assert _followup_allowed(db, followup) == (False, "FILA_SEM_IA")
            binding.active = True
            db.flush()

            # Cliente desistiu: nada de "Qual seu CPF?" depois disso.
            _conversation, declined, _created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-CLOSER-PAUSE-2",
                    "senderName": "Cliente",
                    "text": {"message": "Não preciso mais"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.flush()
            assert _followup_allowed(db, followup) == (False, "CLIENTE_DESISTIU")
            assert (
                schedule_followup(
                    db,
                    agent_id=closer.id,
                    queue_id=queue.id,
                    conversation_id=conversation.id,
                    source_message_id=declined.id,
                )
                is None
            )
        finally:
            db.rollback()
            db.close()


def test_fila_ia_quando_nao_houver_vendedor_online(monkeypatch):
    from app.models import LeadQueuePreference
    from app.services.whatsapp_presence import leave_whatsapp

    auto = replace(settings, ai_autonomous_enabled=True)
    monkeypatch.setattr("app.services.ai_sdr.settings", auto)
    monkeypatch.setattr("app.services.conversation_engine.settings", auto)

    def _inbound(db, queue, phone, message_id, text):
        lead = db.scalar(select(Lead).where(Lead.phone == phone))
        if lead is None:
            lead = Lead(
                business_name=f"Cliente {phone}",
                contact_name="Cliente",
                phone=phone,
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST_AI_OFFLINE"))
            db.commit()
        conversation, message, _created = store_received_payload(
            db,
            {
                "phone": phone,
                "messageId": message_id,
                "senderName": "Cliente",
                "text": {"message": text},
                "fromMe": False,
                "status": "RECEIVED",
            },
        )
        db.commit()
        job = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
        return conversation, job

    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            agent, queue, human = _create_bound_digital_queue(db, "OFFLINE")
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id))
            binding.handling_mode = "AI_WHEN_OFFLINE"
            db.commit()

            # Vendedor online: o cliente novo vai para ele, a IA não entra.
            conversation, job = _inbound(db, queue, "5555988877588", "AI-OFFLINE-1", "Oi, quero internet.")
            assert job is None
            assert conversation.assigned_user_id == human.id

            # O que a IA já começou continua com ela, mesmo com vendedor online.
            started, _job = _inbound(db, queue, "5555988877599", "AI-OFFLINE-2", "Oi")
            db.add(AIConversationState(conversation_id=started.id, agent_id=agent.id, state="AI_ACTIVE"))
            db.commit()
            _same, job = _inbound(db, queue, "5555988877599", "AI-OFFLINE-3", "Quero o de 800 Mega")
            assert job is not None

            # Sem vendedor online: a IA assume o cliente novo e ninguém é atribuído.
            leave_whatsapp(db, human)
            db.commit()
            conversation, job = _inbound(db, queue, "5555988877611", "AI-OFFLINE-4", "Boa noite, tem fibra?")
            assert job is not None
            assert job.agent_id == agent.id
            assert conversation.assigned_user_id is None
        finally:
            db.rollback()
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
        "Móvel",
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
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
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
                    "Escolha uma opção para continuar:"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert [item.label for item in result.quick_replies] == [
        "7GB - R$ 47,90",
        "10GB - R$ 53,90",
        "Mais planos",
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
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
        "Mais planos",
    ]


def test_fiber_catalog_city_reply_is_recognized_without_model_extraction():
    decision = _base_catalog_decision()
    decision.qualification.city = None
    context = {
        "latest_customer_message": "Santa Rosa",
        "lead": {"city": ""},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Claro! Para eu mostrar somente os planos de fibra óptica disponíveis, "
                    "qual é a sua cidade?"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "OFFERING"
    assert result.qualification.city == "Santa Rosa"
    assert [item.label for item in result.quick_replies] == ["800 Mega", "650 Mega"]


def test_selecting_fiber_plan_from_catalog_moves_to_contract_cta_without_handoff():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "800 Mega",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Em Santa Rosa, os planos de fibra óptica disponíveis são "
                    "800 Mega por R$ 119,90 e 650 Mega por R$ 99,90 mensais. "
                    "Qual você quer conhecer?"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "OFFERING"
    assert result.offer_plan_code == "FTTH_800"
    assert result.offer_price == 119.90
    assert "800 Mega" in result.reply_text
    assert "R$ 119,90" in result.reply_text
    assert "contratar esse plano agora" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Quero contratar",
        "Ver outros planos",
    ]


def test_selecting_mobile_plan_from_catalog_moves_to_contract_cta_without_handoff():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "5GB - R$ 44,90",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "No Sulmóvel temos planos nacionais com valores confirmados. "
                    "Escolha uma opção para continuar:"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.sales_state == "OFFERING"
    assert result.offer_plan_code == "SULMOVEL_5GB"
    assert result.offer_price == 44.90
    assert "Sulmóvel 5GB" in result.reply_text
    assert "R$ 44,90" in result.reply_text
    assert "Quer contratar esse plano?" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Quero contratar",
        "Ver outros planos",
    ]


def test_ver_outros_planos_after_mobile_selection_returns_mobile_catalog():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Ver outros planos",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Legal! O Sulmóvel 5GB custa R$ 44,90 mensais. "
                    "Quer contratar esse plano?"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert [item.label for item in result.quick_replies] == [
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
        "Mais planos",
    ]


def test_ver_outros_planos_after_fiber_selection_returns_city_catalog():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Ver outros planos",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Legal! Em Santa Rosa temos o plano de fibra óptica de 800 Mega "
                    "por R$ 119,90 mensais. Quer aproveitar e contratar esse plano agora?"
                ),
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert "800 Mega por R$ 119,90" in result.reply_text
    assert "650 Mega por R$ 99,90" in result.reply_text
    assert [item.label for item in result.quick_replies] == ["800 Mega", "650 Mega"]


def test_current_mobile_allowance_is_not_misread_as_catalog_selection():
    decision = AIDecision(
        reply_text="Entendi. Você quer aumentar sua franquia atual?",
        intent="QUALIFYING",
        qualification_score=45,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente informou franquia atual.",
        next_action="Entender necessidade de dados móveis.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="DISCOVERY",
    )
    context = {
        "latest_customer_message": "Eu tenho 5GB hoje no meu celular",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "INBOUND",
                "body": "Eu tenho 5GB hoje no meu celular",
            }
        ],
    }

    result = _apply_catalog_navigation_policy(decision, context)

    assert result.reply_text == "Entendi. Você quer aumentar sua franquia atual?"
    assert result.offer_plan_code is None


def _base_router_question_decision():
    return AIDecision(
        reply_text="Preciso validar quantos roteadores estão incluídos.",
        intent="INFORMATION",
        qualification_score=55,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.8,
        summary="Cliente perguntou sobre roteadores.",
        next_action="Validar informação.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="OFFERING",
        offer_plan_code="FTTH_650",
        offer_price=99.90,
        unanswered_question="Quantos roteadores acompanham o plano de 650 Mega?",
    )


def test_router_question_for_650_answers_directly_without_validation():
    decision = _base_router_question_decision()
    context = {
        "latest_customer_message": "Roteadores",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Plano de 650 Mega por R$ 99,90 mensais.",
            },
            {
                "direction": "INBOUND",
                "body": "Roteadores",
            },
        ],
    }

    result = _apply_router_information_policy(decision, context)

    assert result.handoff_recommended is False
    assert result.offer_plan_code == "FTTH_650"
    assert result.offer_price == 99.90
    assert result.unanswered_question is None
    assert "1 roteador em comodato" in result.reply_text
    assert "R$ 29,90 por mês" in result.reply_text
    assert "validar" not in result.reply_text.lower()
    assert [item.label for item in result.quick_replies] == [
        "Quero contratar",
        "Tenho outra dúvida",
    ]


def test_router_and_fiber_question_answers_both_points_for_650():
    decision = _base_router_question_decision()
    context = {
        "latest_customer_message": (
            "A internet é fibra? Vem apenas um roteador ou tem mais inclusos?"
        ),
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Plano de 650 Mega por R$ 99,90 mensais.",
            }
        ],
    }

    result = _apply_router_information_policy(decision, context)

    assert "é de fibra óptica" in result.reply_text
    assert "1 roteador em comodato" in result.reply_text
    assert "cada roteador adicional é locado por R$ 29,90 por mês" in result.reply_text
    assert "Quer seguir com o plano de 650 Mega por R$ 99,90 mensais?" in result.reply_text


def test_router_question_for_800_uses_same_confirmed_rule():
    decision = _base_router_question_decision()
    decision.offer_plan_code = "FTTH_800"
    decision.offer_price = 119.90
    context = {
        "latest_customer_message": "Quantos roteadores vêm inclusos?",
        "lead": {"city": "Santa Rosa"},
        "history": [],
    }

    result = _apply_router_information_policy(decision, context)

    assert "plano de 800 Mega inclui 1 roteador em comodato" in result.reply_text
    assert "R$ 29,90 por mês" in result.reply_text
    assert result.offer_price == 119.90


def test_router_question_with_unsupported_explicit_speed_keeps_unavailable_plan_answer():
    decision = AIDecision(
        reply_text=(
            "O plano de 850 Mega não está disponível em Santa Rosa. "
            "Hoje temos 800 Mega por R$ 119,90 ou 650 Mega por R$ 99,90 mensais."
        ),
        intent="INTEREST",
        qualification_score=55,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente pediu plano indisponível.",
        next_action="Oferecer alternativas válidas.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="OFFERING",
    )
    context = {
        "latest_customer_message": "O plano de 850 Mega vem com quantos roteadores?",
        "lead": {"city": "Santa Rosa"},
        "history": [],
    }

    result = _apply_router_information_policy(decision, context)

    assert "850 Mega não está disponível" in result.reply_text
    assert "1 roteador" not in result.reply_text



def test_followup_initial_cadence_is_2h_3h_3h_then_6h():
    assert followup_spec(1)[0].total_seconds() == 2 * 60 * 60
    assert followup_spec(2)[0].total_seconds() == 3 * 60 * 60
    assert followup_spec(3)[0].total_seconds() == 3 * 60 * 60
    assert followup_spec(4)[0].total_seconds() == 6 * 60 * 60


def test_followup_duplicate_detection_blocks_same_message():
    previous = [
        "Quer seguir com o plano de 650 Mega por R$ 99,90 mensais?",
        "Posso te explicar os benefícios do plano?",
    ]

    assert _followup_is_repetitive(
        "Quer seguir com o plano de 650 Mega por R$ 99,90 mensais?",
        previous,
    ) is True


def test_followup_duplicate_detection_blocks_near_identical_message():
    previous = [
        "Quer seguir com o plano de 650 Mega por R$ 99,90 mensais?",
    ]

    assert _followup_is_repetitive(
        "Quer seguir com o plano de 650 Mega por R$ 99,90 por mês?",
        previous,
    ) is True


def test_followup_unique_fallback_avoids_recent_messages():
    previous = [
        "Posso te ajudar por outro ângulo: qual ponto é mais importante para você decidir?",
        "Ficou alguma dúvida específica que eu possa esclarecer para facilitar sua decisão?",
    ]

    result = _unique_followup_fallback(
        step=1,
        previous_messages=previous,
    )

    assert result.reply_text not in previous
    assert _followup_is_repetitive(result.reply_text, previous) is False



def _base_corporate_decision():
    return AIDecision(
        reply_text="Temos o plano residencial de 800 Mega por R$ 119,90.",
        intent="INTEREST",
        qualification_score=50,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente busca internet.",
        next_action="Apresentar oferta.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="OFFERING",
        offer_plan_code="FTTH_800",
        offer_price=119.90,
    )


def test_company_selection_opens_corporate_catalog_and_blocks_residential_offer():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Para empresa",
        "lead": {"city": "Santa Rosa", "confirmed_customer_type": ""},
        "history": [
            {"direction": "INBOUND", "body": "Para empresa"},
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.qualification.customer_type == "EMPRESA"
    assert result.qualification.desired_product == "Internet corporativa"
    assert result.offer_plan_code is None
    assert result.offer_price is None
    assert result.free_months == 0
    assert "planos corporativos de fibra óptica" in result.reply_text
    assert "200 Mega por R$ 139,00" in result.reply_text
    assert "250 Mega por R$ 169,00" in result.reply_text
    assert "650 Mega por R$ 199,90" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Corp. 200 Mega",
        "Corp. 250 Mega",
        "Corp. 650 Mega",
    ]


def test_selecting_corporate_650_uses_business_price_and_benefits():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Corp. 650 Mega",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Para empresa temos planos corporativos de fibra óptica: "
                    "200 Mega, 250 Mega e 650 Mega."
                ),
            },
            {"direction": "INBOUND", "body": "Corp. 650 Mega"},
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.qualification.customer_type == "EMPRESA"
    assert result.offer_plan_code == "CORP_650"
    assert result.offer_price == 199.90
    assert result.free_months == 0
    assert result.concession_level == 0
    assert "650/200 Mbps" in result.reply_text
    assert "IP Fixo" in result.reply_text
    assert "Sulnet Monitora" in result.reply_text
    assert "Roteador Wi-Fi 6" in result.reply_text
    assert "Ativação gratuita" in result.reply_text
    assert "R$ 199,90" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Quero contratar",
        "Ver outros planos",
    ]


def test_confirmed_company_memory_reuses_corporate_catalog_on_future_plan_request():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Quais planos vocês têm?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {"direction": "INBOUND", "body": "Quais planos vocês têm?"},
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.qualification.customer_type == "EMPRESA"
    assert "R$ 139,00" in result.reply_text
    assert "R$ 169,00" in result.reply_text
    assert "R$ 199,90" in result.reply_text
    assert "R$ 119,90" not in result.reply_text
    assert "R$ 99,90" not in result.reply_text


def test_persist_confirmed_company_type_updates_crm_segment_and_raw_memory():
    lead = Lead(
        business_name="",
        contact_name="Cliente Empresa",
        phone="5555999999999",
        segment="Não informado",
        city="Santa Rosa",
        source="MANUAL",
        origin_channel="WHATSAPP",
        team="DIGITAL",
        stage="NOVO",
        score=0,
        temperature="FRIO",
        priority="NORMAL",
        raw_data='{"origem":"teste"}',
    )

    _persist_confirmed_customer_type(
        lead,
        "EMPRESA",
        conversation_id=123,
    )

    assert lead.segment == "Empresa"
    assert _confirmed_customer_type_from_lead(lead) == "EMPRESA"
    assert '"origem": "teste"' in lead.raw_data
    assert '"source": "INBOUND_CUSTOMER_CONFIRMED"' in lead.raw_data


def test_residential_confirmation_can_replace_previous_company_context():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Agora é para casa",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {"direction": "INBOUND", "body": "Para empresa"},
            {"direction": "INBOUND", "body": "Agora é para casa"},
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.qualification.customer_type == "RESIDENCIAL"



def test_company_difference_question_explains_business_value_instead_of_reopening_catalog():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Qual a diferença do plano empresa para o residencial?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {"direction": "INBOUND", "body": "Para empresa"},
            {
                "direction": "OUTBOUND",
                "body": "Qual plano corporativo você quer conhecer?",
            },
            {
                "direction": "INBOUND",
                "body": "Qual a diferença do plano empresa para o residencial?",
            },
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.qualification.customer_type == "EMPRESA"
    assert "SLA de atendimento de até 8 horas" in result.reply_text
    assert "suporte nível 2" in result.reply_text
    assert "equipe especializada em empresas" in result.reply_text
    assert "não é compartilhada da mesma forma que no residencial" in result.reply_text
    assert "IP fixo" in result.reply_text
    assert "Sulnet Monitora" in result.reply_text
    assert "NOC" in result.reply_text
    assert "R$ 139,00" not in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Ver planos",
        "Outra dúvida",
    ]


def test_corporate_sla_is_presented_as_service_sla_not_resolution_deadline():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Qual o SLA de vocês para empresa?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert "SLA de atendimento de até 8 horas" in result.reply_text
    assert "resolução em até 8 horas" not in result.reply_text.lower()
    assert "resolve em até 8 horas" not in result.reply_text.lower()


def test_corporate_band_question_does_not_claim_dedicated_link():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "É link dedicado ou a banda é compartilhada?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert "entrega da banda é diferenciada" in result.reply_text
    assert "não é compartilhada da mesma forma que no residencial" in result.reply_text
    assert "link dedicado" not in result.reply_text.lower()


def test_corporate_monitoring_question_explains_proactive_noc():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "O que é o Sulnet Monitora?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert "Sulnet Monitora" in result.reply_text
    assert "NOC acompanhando quedas ou oscilações" in result.reply_text
    assert "atuando de forma proativa" in result.reply_text


def test_corporate_200_now_includes_monitora_in_structured_offer():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Corp. 200 Mega",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Para empresa temos os planos Corp. 200, Corp. 250 e Corp. 650.",
            }
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert result.offer_plan_code == "CORP_200"
    assert result.offer_price == 139.00
    assert "Sulnet Monitora" in result.reply_text
    assert "IP Fixo" in result.reply_text


def test_ver_planos_after_corporate_differentials_returns_corporate_catalog():
    decision = _base_corporate_decision()
    context = {
        "latest_customer_message": "Ver planos",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_customer_type": "EMPRESA",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "O link corporativo tem SLA de atendimento de até 8 horas, "
                    "suporte nível 2 e Sulnet Monitora. Quer ver as opções corporativas?"
                ),
            }
        ],
    }

    result = _apply_corporate_customer_policy(decision, context)

    assert "200 Mega por R$ 139,00" in result.reply_text
    assert "250 Mega por R$ 169,00" in result.reply_text
    assert "650 Mega por R$ 199,90" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Corp. 200 Mega",
        "Corp. 250 Mega",
        "Corp. 650 Mega",
    ]



def _base_prospecting_entry_decision():
    return AIDecision(
        reply_text="Em Santa Rosa temos 800 Mega por R$ 119,90.",
        intent="INTEREST",
        qualification_score=45,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente demonstrou interesse.",
        next_action="Apresentar oferta.",
        qualification=QualificationData(city="Santa Rosa"),
        crm_notes=[],
        sales_state="OFFERING",
        offer_plan_code="FTTH_800",
        offer_price=119.90,
    )


def test_first_prospecting_question_is_existing_customer_before_any_offer():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Quero saber os planos de internet",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
        },
        "history": [
            {
                "direction": "INBOUND",
                "body": "Quero saber os planos de internet",
            }
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "NAO_INFORMADO"
    assert result.offer_plan_code is None
    assert result.offer_price is None
    assert result.sales_state == "DISCOVERY"
    assert result.reply_text == "Antes de te mostrar as opções, você já é cliente Sulnet?"
    assert [item.label for item in result.quick_replies] == ["Sim", "Não"]


def test_existing_customer_confirmation_routes_to_mobile_or_new_connection(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: False)
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Sim",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Antes de te mostrar as opções, você já é cliente Sulnet?",
            },
            {"direction": "INBOUND", "body": "Sim"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "CLIENTE"
    assert result.offer_plan_code is None
    assert "Telefonia Móvel" in result.reply_text
    assert "outro endereço" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Telefonia Móvel",
        "Nova conexão",
        "Upgrade ou suporte",
    ]


def test_new_customer_confirmation_routes_to_internet_or_mobile(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: False)
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Não",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Antes de te mostrar as opções, você já é cliente Sulnet?",
            },
            {"direction": "INBOUND", "body": "Não"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "NAO_CLIENTE"
    assert result.offer_plan_code is None
    assert "planos de internet ou Telefonia Móvel" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Internet",
        "Telefonia Móvel",
    ]


def test_new_customer_internet_choice_asks_home_or_company():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Internet",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "O que você quer conhecer: planos de internet ou Telefonia Móvel?",
            },
            {"direction": "INBOUND", "body": "Internet"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "NAO_CLIENTE"
    assert result.qualification.desired_product == "Internet"
    assert result.reply_text == "A internet é para sua casa ou empresa?"
    assert [item.label for item in result.quick_replies] == [
        "Para casa",
        "Para empresa",
    ]


def test_alternative_existing_customer_question_recognizes_no_from_history():
    history = [
        {
            "direction": "OUTBOUND",
            "body": "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?",
        },
        {"direction": "INBOUND", "body": "Não"},
    ]

    facts = _confirmed_facts_from_history(history)

    assert facts["existing_customer"] == "NAO_CLIENTE"
    assert "EXISTING_CUSTOMER" in facts["answered_questions"]


def test_conversation_without_lead_does_not_regress_after_internet_choice():
    history = [
        {
            "direction": "OUTBOUND",
            "body": "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?",
        },
        {"direction": "INBOUND", "body": "Não"},
        {
            "direction": "OUTBOUND",
            "body": "Qual caminho você quer seguir primeiro: Internet ou Telefonia Móvel?",
        },
        {"direction": "INBOUND", "body": "Internet"},
    ]
    facts = _confirmed_facts_from_history(history)
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Internet",
        "confirmed_facts": facts,
        "lead": {
            "city": "",
            "confirmed_existing_customer": "",
            "confirmed_customer_type": "",
            "journey": {},
        },
        "history": history,
    }

    result = _apply_prospecting_entry_policy(decision, context)
    result = _apply_journey_orchestration_policy(result, context)

    assert facts["existing_customer"] == "NAO_CLIENTE"
    assert facts["product_kind"] == "INTERNET"
    assert result.qualification.existing_customer == "NAO_CLIENTE"
    assert result.journey_state == "CHOOSE_SEGMENT"
    assert (
        "casa ou empresa" in result.reply_text.lower()
        or "residencial ou empresarial" in result.reply_text.lower()
    )
    assert [item.label for item in result.quick_replies] == [
        "Para casa",
        "Para empresa",
    ]


def test_integrity_guard_flags_regression_to_answered_existing_customer_gate():
    decision = _base_prospecting_entry_decision()
    decision.journey_state = "IDENTIFY_CUSTOMER"
    decision.reply_text = (
        "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"
    )
    context = {
        "latest_customer_message": "Internet",
        "confirmed_facts": {
            "existing_customer": "NAO_CLIENTE",
            "customer_type": "",
            "product_kind": "INTERNET",
            "desired_product": "Internet",
            "city": "",
            "answered_questions": ["EXISTING_CUSTOMER", "PRODUCT"],
        },
        "lead": {
            "city": "",
            "confirmed_existing_customer": "",
            "confirmed_customer_type": "",
            "journey": {},
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Você já possui algum serviço da Sulnet?",
            },
            {"direction": "INBOUND", "body": "Não"},
            {
                "direction": "OUTBOUND",
                "body": "Qual caminho você quer seguir primeiro: Internet ou Telefonia Móvel?",
            },
            {"direction": "INBOUND", "body": "Internet"},
        ],
    }

    issues = _journey_integrity_issues(decision, context)

    assert "STATE_REGRESSION_EXISTING_CUSTOMER" in issues
    assert "IGNORED_CONFIRMED_EXISTING_CUSTOMER" in issues


def test_existing_customer_new_connection_asks_home_or_company_for_new_address():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Nova conexão",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Para uma nova contratação, você quer Telefonia Móvel "
                    "ou uma nova conexão para outro endereço?"
                ),
            },
            {"direction": "INBOUND", "body": "Nova conexão"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "CLIENTE"
    assert result.qualification.desired_product == "Nova conexão de internet"
    assert "outro endereço" in result.reply_text
    assert [item.label for item in result.quick_replies] == [
        "Para casa",
        "Para empresa",
    ]


def test_existing_customer_mobile_choice_opens_mobile_catalog():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Telefonia Móvel",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "CLIENTE",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Para uma nova contratação, você quer Telefonia Móvel "
                    "ou uma nova conexão para outro endereço?"
                ),
            }
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "CLIENTE"
    assert result.qualification.desired_product == "Sulmóvel"
    assert result.sales_state == "OFFERING"
    assert [item.label for item in result.quick_replies] == [
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
        "Mais planos",
    ]


def test_existing_customer_memory_is_persisted_without_overwriting_other_raw_data():
    lead = Lead(
        business_name="",
        contact_name="Cliente",
        phone="5555999999999",
        segment="Residencial",
        city="Santa Rosa",
        source="MANUAL",
        origin_channel="WHATSAPP",
        team="DIGITAL",
        stage="NOVO",
        score=0,
        temperature="FRIO",
        priority="NORMAL",
        raw_data='{"origem":"campanha"}',
    )

    _persist_confirmed_existing_customer(
        lead,
        "CLIENTE",
        conversation_id=321,
    )

    assert _confirmed_existing_customer_from_lead(lead) == "CLIENTE"
    assert '"origem": "campanha"' in lead.raw_data
    assert '"source": "INBOUND_CUSTOMER_CONFIRMED"' in lead.raw_data


def test_simple_no_without_customer_question_does_not_confirm_not_customer():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Não",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Quer que eu te explique os benefícios?",
            },
            {"direction": "INBOUND", "body": "Não"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "NAO_INFORMADO"
    assert result.reply_text == "Antes de te mostrar as opções, você já é cliente Sulnet?"


def test_competitor_customer_phrase_is_not_misread_as_sulnet_customer():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Sou cliente da Vivo hoje",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
        },
        "history": [
            {"direction": "INBOUND", "body": "Sou cliente da Vivo hoje"},
        ],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.qualification.existing_customer == "NAO_INFORMADO"
    assert result.reply_text == "Antes de te mostrar as opções, você já é cliente Sulnet?"


def test_opt_out_is_not_overridden_by_prospecting_customer_gate():
    decision = AIDecision(
        reply_text="Tudo bem. Não enviaremos novas mensagens.",
        intent="DO_NOT_CONTACT",
        qualification_score=0,
        temperature="FRIO",
        recommended_stage="NAO_CONTATAR",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.99,
        summary="Cliente pediu para não receber mais mensagens.",
        next_action="Encerrar contato.",
        qualification=QualificationData(),
        crm_notes=[],
        sales_state="OPT_OUT",
        followup_needed=False,
    )
    context = {
        "latest_customer_message": "Não quero mais mensagens",
        "lead": {"confirmed_existing_customer": ""},
        "history": [],
    }

    result = _apply_prospecting_entry_policy(decision, context)

    assert result.intent == "DO_NOT_CONTACT"
    assert result.sales_state == "OPT_OUT"
    assert result.reply_text == "Tudo bem. Não enviaremos novas mensagens."



def _base_journey_decision():
    return AIDecision(
        reply_text="Temos o plano de 800 Mega por R$ 119,90. Quer contratar?",
        intent="INTEREST",
        qualification_score=65,
        temperature="MORNO",
        recommended_stage="PROPOSTA",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.9,
        summary="Cliente em negociação.",
        next_action="Avançar venda.",
        qualification=QualificationData(
            city="Santa Rosa",
            customer_type="RESIDENCIAL",
            existing_customer="NAO_CLIENTE",
            desired_product="Internet",
        ),
        crm_notes=[],
        sales_state="OFFERING",
        offer_plan_code="FTTH_800",
        offer_price=119.90,
    )


def test_explicit_purchase_intent_routes_to_closer_ai_not_human():
    decision = _base_journey_decision()
    decision.intent = "HUMAN_REQUEST"
    decision.handoff_recommended = True
    decision.sales_state = "READY_FOR_HUMAN_SALES"
    context = {
        "latest_customer_message": "Quero contratar esse plano",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {},
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Quer contratar o plano de 800 Mega por R$ 119,90?",
            },
            {"direction": "INBOUND", "body": "Quero contratar esse plano"},
        ],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CLOSING"
    assert result.handoff_target == "CLOSER_AI"
    assert result.handoff_recommended is False
    assert result.customer_commitment is True
    assert result.sales_state == "PURCHASE_INTENT"
    assert result.intent == "HIGH_INTENT"


def test_short_yes_after_purchase_cta_routes_to_closer_ai():
    decision = _base_journey_decision()
    context = {
        "latest_customer_message": "Sim",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {},
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Quer contratar esse plano agora?",
            },
            {"direction": "INBOUND", "body": "Sim"},
        ],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CLOSING"
    assert result.handoff_target == "CLOSER_AI"
    assert result.customer_commitment is True


def test_model_only_purchase_intent_does_not_start_closer_without_customer_evidence():
    decision = _base_journey_decision()
    decision.customer_commitment = True
    decision.sales_state = "PURCHASE_INTENT"
    decision.intent = "HIGH_INTENT"
    context = {
        "latest_customer_message": "Quais as vantagens desse plano?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {},
        },
        "history": [
            {"direction": "INBOUND", "body": "Quais as vantagens desse plano?"},
        ],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CONFIRM_PURCHASE"
    assert result.handoff_target == "NONE"
    assert result.customer_commitment is False
    assert result.sales_state == "OFFERING"


def test_explicit_human_request_routes_only_to_human():
    decision = _base_journey_decision()
    context = {
        "latest_customer_message": "Quero falar com uma pessoa",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {},
        },
        "history": [],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "HUMAN_HANDOFF"
    assert result.handoff_target == "HUMAN"
    assert result.handoff_recommended is True
    assert result.intent == "HUMAN_REQUEST"
    assert result.followup_needed is False


def test_model_cannot_handoff_human_without_explicit_human_request():
    decision = _base_journey_decision()
    decision.handoff_recommended = True
    decision.handoff_reason = "Modelo decidiu escalar."
    decision.intent = "HUMAN_REQUEST"
    decision.sales_state = "READY_FOR_HUMAN_SALES"
    context = {
        "latest_customer_message": "Me explica melhor o plano",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {},
        },
        "history": [],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.handoff_target == "NONE"
    assert result.handoff_recommended is False
    assert result.handoff_reason == ""
    assert result.intent != "HUMAN_REQUEST"
    assert result.sales_state != "READY_FOR_HUMAN_SALES"


def test_journey_gate_blocks_offer_before_existing_customer_is_known():
    decision = _base_journey_decision()
    context = {
        "latest_customer_message": "Quanto custa 800 Mega?",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "",
            "confirmed_customer_type": "",
            "journey": {},
        },
        "history": [],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "IDENTIFY_CUSTOMER"
    assert result.offer_plan_code is None
    assert result.offer_price is None
    assert "cliente Sulnet" in result.reply_text
    assert [item.label for item in result.quick_replies] == ["Sim", "Não"]


def test_journey_gate_requires_segment_before_internet_offer():
    decision = _base_journey_decision()
    decision.qualification.customer_type = "NAO_INFORMADO"
    decision.offer_plan_code = None
    decision.offer_price = None
    context = {
        "latest_customer_message": "Internet",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "",
            "journey": {"desired_product": "Internet"},
        },
        "history": [],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CHOOSE_SEGMENT"
    assert "casa ou empresa" in result.reply_text.lower()
    assert [item.label for item in result.quick_replies] == [
        "Para casa",
        "Para empresa",
    ]


def test_residential_internet_without_city_is_forced_to_confirm_city():
    decision = _base_journey_decision()
    decision.qualification.city = None
    decision.offer_plan_code = None
    decision.offer_price = None
    context = {
        "latest_customer_message": "Para casa",
        "lead": {
            "city": "",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {"desired_product": "Internet"},
        },
        "history": [],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CONFIRM_CITY"
    assert "cidade" in result.reply_text.lower()
    assert result.offer_plan_code is None


def test_main_sdr_repetition_detector_catches_near_duplicate_cta():
    context = {
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Quer seguir com o plano de 650 Mega por R$ 99,90 mensais?",
            }
        ]
    }
    decision = _base_journey_decision()
    decision.reply_text = "Quer seguir com o plano de 650 Mega por R$ 99,90 por mês?"

    assert _sdr_reply_repeats_recent_outbound(decision, context) is True


def test_stagnation_changes_strategy_after_repeated_same_stage():
    decision = _base_journey_decision()
    decision.objection = "OTHER"
    decision.sales_state = "NEGOTIATING"
    context = {
        "latest_customer_message": "Ainda estou pensando",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "NAO_CLIENTE",
            "confirmed_customer_type": "RESIDENCIAL",
            "journey": {
                "state": "HANDLE_OBJECTION",
                "pending_action": "Resolver a objeção atual com um ângulo novo e avançar a decisão.",
                "desired_product": "Internet",
                "offer_plan_code": "FTTH_800",
                "stagnation_count": 3,
            },
        },
        "history": [
            {
                "direction": "OUTBOUND",
                "body": "Temos o plano de 800 Mega por R$ 119,90. Quer contratar?",
            },
            {"direction": "INBOUND", "body": "Ainda estou pensando"},
        ],
    }

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "HANDLE_OBJECTION"
    assert "o que mais pesa" in result.reply_text.lower()
    assert [item.label for item in result.quick_replies] == [
        "Preço",
        "Qualidade",
        "Momento",
    ]


def test_journey_memory_is_structured_and_isolated_by_conversation():
    lead = Lead(
        business_name="",
        contact_name="Teste Jornada",
        phone="5555999999999",
        segment="Residencial",
        city="Santa Rosa",
        source="MANUAL",
        origin_channel="WHATSAPP",
        team="DIGITAL",
        stage="PROPOSTA",
        score=0,
        temperature="MORNO",
        priority="NORMAL",
        raw_data='{"origem":"teste"}',
    )
    decision = _base_journey_decision()
    decision.journey_state = "CONFIRM_PURCHASE"
    decision.journey_pending_action = "Obter aceite explícito para contratar a oferta escolhida."
    decision.stagnation_count = 2

    _persist_journey_memory(lead, decision, conversation_id=999)

    memory = _journey_memory_from_lead(lead, conversation_id=999)
    assert memory["state"] == "CONFIRM_PURCHASE"
    assert memory["offer_plan_code"] == "FTTH_800"
    assert memory["stagnation_count"] == 2
    assert _journey_memory_from_lead(lead, conversation_id=1000) == {}
    assert '"origem": "teste"' in lead.raw_data


def test_journey_memory_does_not_erase_confirmed_gate_facts():
    lead = Lead(
        business_name="",
        contact_name="Teste Memória",
        phone="5555888888888",
        segment="Residencial",
        city="Santa Rosa",
        source="MANUAL",
        origin_channel="WHATSAPP",
        team="DIGITAL",
        stage="CONTATO_REALIZADO",
        score=0,
        temperature="FRIO",
        priority="NORMAL",
        raw_data="{}",
    )
    first = _base_journey_decision()
    first.journey_state = "CHOOSE_SEGMENT"
    first.qualification.existing_customer = "NAO_CLIENTE"
    first.qualification.desired_product = "Internet"
    first.qualification.customer_type = "NAO_INFORMADO"
    _persist_journey_memory(lead, first, conversation_id=77)

    second = _base_journey_decision()
    second.journey_state = "CHOOSE_PRODUCT"
    second.qualification.existing_customer = "NAO_INFORMADO"
    second.qualification.desired_product = None
    second.qualification.customer_type = "NAO_INFORMADO"
    _persist_journey_memory(lead, second, conversation_id=77)

    memory = _journey_memory_from_lead(lead, conversation_id=77)
    assert memory["existing_customer"] == "NAO_CLIENTE"
    assert memory["desired_product"] == "Internet"


def test_followup_theme_recovers_current_pending_action():
    theme = (
        "reativação 1: retomar o contexto. "
        "Pendência atual: Confirmar se a conexão é para casa ou empresa."
    )

    assert _pending_action_from_theme(theme) == (
        "Confirmar se a conexão é para casa ou empresa."
    )


def test_existing_customer_plan_change_redirects_to_service_channel():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": "Queria aumentar meu plano",
        "lead": {
            "city": "Santa Rosa",
            "confirmed_existing_customer": "CLIENTE",
        },
        "history": [
            {"direction": "INBOUND", "body": "Já tenho um plano"},
            {"direction": "INBOUND", "body": "Queria aumentar"},
        ],
    }

    result = _apply_existing_customer_plan_change_policy(decision, context)

    assert result.service_redirect is True
    assert result.qualification.existing_customer == "CLIENTE"
    assert result.handoff_target == "NONE"
    assert result.followup_needed is False
    assert result.offer_plan_code is None
    assert "(55) 3520-8000" in result.reply_text
    assert "exclusivo para novas contratações" in result.reply_text


def test_purchase_intent_tenho_interesse_em_assinar_enters_closing():
    decision = _base_prospecting_entry_decision()
    context = {
        "latest_customer_message": (
            "Olá! Tenho interesse em assinar 650mb por R$99,90 da Sulnet!"
        ),
        "history": [
            {
                "direction": "INBOUND",
                "body": "Olá! Tenho interesse em assinar 650mb por R$99,90 da Sulnet!",
            }
        ],
        "lead": {},
    }

    assert _explicit_purchase_commitment(context) is True

    result = _apply_journey_orchestration_policy(decision, context)

    assert result.journey_state == "CLOSING"
    assert result.handoff_target == "CLOSER_AI"
    assert result.customer_commitment is True
    assert result.sales_state == "PURCHASE_INTENT"
    assert result.followup_needed is False


def test_purchase_intent_quero_assinar_enters_closing():
    assert _explicit_purchase_commitment(
        {"latest_customer_message": "quero assinar esse plano"}
    ) is True
    assert _explicit_purchase_commitment(
        {"latest_customer_message": "gostaria de assinar 650 mega"}
    ) is True
    assert _explicit_purchase_commitment(
        {"latest_customer_message": "vou assinar o plano"}
    ) is True


def test_purchase_intent_negative_phrase_does_not_close():
    assert _explicit_purchase_commitment(
        {"latest_customer_message": "não tenho interesse em assinar"}
    ) is False
    assert _explicit_purchase_commitment(
        {"latest_customer_message": "não quero contratar agora"}
    ) is False


def test_sdr_cannot_expose_internal_closer_handoff():
    assert _sdr_exposes_internal_handoff(
        "O Vendedor IA dará continuidade à sua contratação."
    ) is True
    assert _sdr_exposes_internal_handoff(
        "Perfeito! Temos 650 Mega por R$ 99,90."
    ) is False


def test_followup_cannot_announce_internal_closer_handoff():
    assert _followup_exposes_internal_handoff(
        "Seu interesse está registrado. O Vendedor IA seguirá com as próximas etapas."
    ) is True
    assert _followup_exposes_internal_handoff(
        "Ficou alguma dúvida sobre o plano?"
    ) is False


_AD_MESSAGE = "Olá! Tenho interesse em assinar 650mb por R$99,90 da Sulnet!"
_FAST_CITY_QUESTION = (
    "Ótimo! Para confirmar a disponibilidade do plano de 650 Mega por R$ 99,90, "
    "em qual cidade será a instalação?"
)


def _fast_context(latest, history, city=""):
    return {
        "latest_customer_message": latest,
        "history": history,
        "lead": {"city": city},
        "confirmed_facts": {},
    }


def test_fast_purchase_asks_city_when_unknown():
    # Conversas 239, 237, 72 e 68: o anúncio chegava pronto e não ia ao fechamento.
    from app.services.ai_sdr import _fast_purchase_decision, _fast_purchase_request

    request = _fast_purchase_request(
        _fast_context(_AD_MESSAGE, [{"direction": "INBOUND", "body": _AD_MESSAGE}])
    )

    assert request is not None
    assert request["plan"]["download_mbps"] == 650
    assert request["city"] == ""
    decision = _fast_purchase_decision(request["plan"], "", handoff=False)
    assert "em qual cidade" in decision.reply_text
    assert decision.handoff_target == "NONE"


def test_fast_purchase_hands_off_when_city_is_known():
    from app.services.ai_sdr import _fast_purchase_decision, _fast_purchase_request

    request = _fast_purchase_request(
        _fast_context(_AD_MESSAGE, [{"direction": "INBOUND", "body": _AD_MESSAGE}], city="Santa Rosa")
    )

    assert request["city"] == "Santa Rosa"
    decision = _fast_purchase_decision(request["plan"], "Santa Rosa", handoff=True)
    assert decision.handoff_target == "CLOSER_AI"
    assert decision.sales_state == "PURCHASE_INTENT"
    assert decision.offer_plan_code == "Fibra 650 Mega"
    assert decision.offer_price == 99.9


def test_fast_purchase_uses_city_answer_after_fast_question():
    from app.services.ai_sdr import _fast_purchase_request

    history = [
        {"direction": "INBOUND", "body": _AD_MESSAGE},
        {"direction": "OUTBOUND", "body": _FAST_CITY_QUESTION},
        {"direction": "INBOUND", "body": "Santo Ângelo"},
    ]
    request = _fast_purchase_request(_fast_context("Santo Ângelo", history))

    assert request["plan"]["download_mbps"] == 650
    assert request["city"] == "Santo Ângelo"


def test_fast_purchase_recognizes_ad_style_interest():
    # Conversa 63: "tenho interesse em 650mb por R$99,90" recebia oferta de 800.
    from app.services.ai_sdr import _fast_purchase_request

    message = "Olá, tenho interesse em 650mb por R$99,90."
    request = _fast_purchase_request(
        _fast_context(message, [{"direction": "INBOUND", "body": message}])
    )

    assert request is not None
    assert request["plan"]["download_mbps"] == 650


def test_fast_purchase_ignores_messages_without_explicit_plan_intent():
    from app.services.ai_sdr import _fast_purchase_request

    for latest, history in (
        ("Quero contratar", [{"direction": "INBOUND", "body": "Quero contratar"}]),
        ("Hoje eu tenho 500mb.", [{"direction": "INBOUND", "body": "Hoje eu tenho 500mb."}]),
        (
            "Santa rosa",
            [
                {"direction": "OUTBOUND", "body": "Em qual cidade será instalada a conexão?"},
                {"direction": "INBOUND", "body": "Santa rosa"},
            ],
        ),
    ):
        assert _fast_purchase_request(_fast_context(latest, history)) is None


def test_burst_of_customer_messages_gets_a_single_sdr_job():
    """Conversa 63: cada mensagem do cliente virava uma resposta separada."""
    with TestClient(app):
        db = SessionLocal()
        try:
            _agent, queue, _human = _create_bound_digital_queue(db, "BURST")
            from app.models import LeadQueuePreference

            lead = Lead(
                business_name="Cliente Rajada",
                contact_name="Cliente",
                phone="5555988877399",
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST_BURST"))
            db.commit()

            jobs = []
            for index, body in enumerate(("Oi", "Quero internet", "Para minha casa")):
                conversation, message, _created = store_received_payload(
                    db,
                    {
                        "phone": lead.phone,
                        "messageId": f"AI-BURST-{index}",
                        "senderName": "Cliente",
                        "text": {"message": body},
                        "fromMe": False,
                        "status": "RECEIVED",
                    },
                )
                db.commit()
                jobs.append(db.scalar(select(AIJob).where(AIJob.message_id == message.id)))

            statuses = [db.get(AIJob, job.id).status for job in jobs if job]
            assert len(statuses) == 3
            assert statuses[:2] == ["IGNORADO", "IGNORADO"]
            assert statuses[2] == "PENDENTE"
        finally:
            db.close()


def _black_history(*pairs):
    return [{"direction": d, "body": b} for d, b in pairs]


def test_black_perguntada_logo_depois_de_ser_cliente(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    expected = {
        "CLIENTE": (
            "Você já está sabendo da Black da Sulnet, que beneficia os nossos clientes?",
            ["Quero saber", "Agora não"],
        ),
        "NAO_CLIENTE": ("Quer conhecer nossa promoção Black da Sulnet?", ["Sim", "Não"]),
    }
    for answer, status in (("Sim", "CLIENTE"), ("Não", "NAO_CLIENTE")):
        context = {
            "latest_customer_message": answer,
            "lead": {"city": "Santa Rosa", "confirmed_existing_customer": ""},
            "history": _black_history(
                ("OUTBOUND", "Antes de te mostrar as opções, você já é cliente Sulnet?"),
                ("INBOUND", answer),
            ),
        }
        result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
        assert result.qualification.existing_customer == status
        assert result.reply_text == expected[status][0]
        assert [item.label for item in result.quick_replies] == expected[status][1]


def test_cliente_sem_interesse_na_black_escolhe_assunto_e_suporte_vai_para_central(monkeypatch):
    from app.services.ai_campaign_black import CUSTOMER_OFFER_QUESTION

    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    lead = {"city": "Santa Rosa", "confirmed_existing_customer": "CLIENTE"}
    context = {
        "latest_customer_message": "Agora não",
        "lead": lead,
        "history": _black_history(("OUTBOUND", CUSTOMER_OFFER_QUESTION), ("INBOUND", "Agora não")),
    }
    result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
    assert [item.label for item in result.quick_replies] == [
        "Telefonia Móvel",
        "Nova conexão",
        "Upgrade ou suporte",
    ]

    for text in ("Upgrade ou suporte", "Preciso da segunda via do boleto", "minha internet está lenta"):
        context = {
            "latest_customer_message": text,
            "lead": lead,
            "history": _black_history(("OUTBOUND", result.reply_text), ("INBOUND", text)),
        }
        redirect = _apply_existing_customer_plan_change_policy(_base_prospecting_entry_decision(), context)
        assert redirect.service_redirect is True, text
        assert "(55) 3520-8000" in redirect.reply_text
        assert redirect.followup_needed is False

    # Quem não é cliente perguntando de suporte segue na venda.
    context = {
        "latest_customer_message": "Como funciona o suporte técnico?",
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "NAO_CLIENTE"},
        "history": [],
    }
    assert _apply_existing_customer_plan_change_policy(
        _base_prospecting_entry_decision(), context
    ).service_redirect is False


def test_central_vai_com_botao_que_abre_whatsapp(monkeypatch):
    from app.services.zapi import ZApiService

    calls = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"messageId": "X"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls["url"] = url
            calls["json"] = json
            return _Resp()

    monkeypatch.setattr("app.services.zapi.httpx.Client", _Client)
    service = ZApiService.__new__(ZApiService)
    monkeypatch.setattr(service, "_url", lambda path: f"https://zapi/{path}", raising=False)
    monkeypatch.setattr(service, "_headers", lambda json_request=False: {}, raising=False)
    monkeypatch.setattr(service, "normalize_phone", lambda phone: phone, raising=False)
    service.send_link_button(phone="5555999", message="Oi", label="Falar com a central", url="https://wa.me/555535208000")
    assert calls["url"].endswith("send-button-actions")
    action = calls["json"]["buttonActions"][0]
    assert action == {"id": "1", "type": "URL", "url": "https://wa.me/555535208000", "label": "Falar com a central"}


def test_black_sim_apresenta_movel_e_combo(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    context = {
        "latest_customer_message": "Sim",
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "NAO_CLIENTE"},
        "history": _black_history(
            ("OUTBOUND", "Quer conhecer nossa promoção Black da Sulnet?"),
            ("INBOUND", "Sim"),
        ),
    }
    result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
    assert "10/01/2027" in result.reply_text
    assert [item.label for item in result.quick_replies] == ["Só móvel", "Combo fibra + móvel"]


def test_black_nao_segue_fluxo_normal(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    context = {
        "latest_customer_message": "Não",
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "NAO_CLIENTE"},
        "history": _black_history(
            ("OUTBOUND", "Quer conhecer nossa promoção Black da Sulnet?"),
            ("INBOUND", "Não"),
        ),
    }
    result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
    assert "planos de internet ou Telefonia Móvel" in result.reply_text


def test_black_so_movel_abre_menu_de_planos(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    context = {
        "latest_customer_message": "Só móvel",
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "NAO_CLIENTE"},
        "history": _black_history(
            ("OUTBOUND", "Black da Sulnet: a Black que pula o fim de ano! Qual você prefere?"),
            ("INBOUND", "Só móvel"),
        ),
    }
    result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
    assert "Sulmóvel" in result.reply_text
    assert [item.label for item in result.quick_replies][:2] == [
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
    ]
    assert all(len(item.label) <= 20 for item in result.quick_replies)


def test_pedido_de_opcoes_depois_de_plano_unico_abre_menu():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Quero conhecer opçõe",
        "lead": {"city": "Santa Rosa"},
        "history": [
            {
                "direction": "OUTBOUND",
                "body": (
                    "Para usar só móvel, o plano com mais internet é o Sulmóvel 15GB "
                    "por R$ 69,80. Quer contratar esse plano agora?"
                ),
            }
        ],
    }
    result = _apply_catalog_navigation_policy(decision, context)
    assert [item.label for item in result.quick_replies][:2] == [
        "2GB - R$ 33,90",
        "5GB - R$ 44,90",
    ]


def test_black_combo_vai_para_internet(monkeypatch):
    monkeypatch.setattr("app.services.ai_campaign_black.black_active", lambda now=None: True)
    context = {
        "latest_customer_message": "Combo fibra + móvel",
        "lead": {"city": "Santa Rosa", "confirmed_existing_customer": "NAO_CLIENTE"},
        "history": _black_history(
            ("OUTBOUND", "Black da Sulnet: a Black que pula o fim de ano! Qual você prefere?"),
            ("INBOUND", "Combo fibra + móvel"),
        ),
    }
    result = _apply_prospecting_entry_policy(_base_prospecting_entry_decision(), context)
    assert "casa ou empresa" in result.reply_text


def test_estagnacao_nao_troca_pergunta_da_black():
    from app.services.ai_sdr import _apply_stagnation_strategy

    decision = _base_prospecting_entry_decision()
    decision.reply_text = "Quer conhecer nossa promoção Black da Sulnet?"
    decision.journey_state = "CHOOSE_PRODUCT"
    context = {"lead": {"confirmed_existing_customer": "NAO_CLIENTE"}, "history": []}
    result = _apply_stagnation_strategy(decision, context)
    assert result.reply_text == "Quer conhecer nossa promoção Black da Sulnet?"


def test_mobile_catalog_third_page_shows_15gb_and_back():
    decision = _base_catalog_decision()
    context = {
        "latest_customer_message": "Mais planos",
        "lead": {"city": "Santa Rosa"},
        "history": [{"direction": "OUTBOUND", "body": "Mais opções do Sulmóvel:"}],
    }
    result = _apply_catalog_navigation_policy(decision, context)
    assert [item.label for item in result.quick_replies] == ["15GB - R$ 69,80", "Voltar"]


def _rejection_decision():
    return AIDecision(
        reply_text="Entendi! E hoje você já tem internet em casa?",
        intent="QUALIFYING",
        qualification_score=20,
        temperature="FRIO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.8,
        summary="Cliente sem interesse.",
        next_action="Entender a situação.",
        qualification=QualificationData(),
        crm_notes=[],
    )


def test_cliente_sem_interesse_recebe_uma_tentativa_e_depois_ia_encerra():
    from app.services.ai_sdr_policies import REJECTION_CLOSE_MESSAGE, _apply_repeated_rejection_policy

    first = {
        "latest_customer_message": "Não tenho interesse",
        "history": [
            {"direction": "OUTBOUND", "body": "Quer conhecer os planos de fibra?"},
            {"direction": "INBOUND", "body": "Não tenho interesse"},
        ],
    }
    decision = _apply_repeated_rejection_policy(_rejection_decision(), first)
    assert decision.reply_text != REJECTION_CLOSE_MESSAGE  # 1ª recusa: uma tentativa

    second = {
        "latest_customer_message": "Não, obrigado",
        "history": first["history"] + [
            {"direction": "OUTBOUND", "body": "Entendi! E hoje você já tem internet em casa?"},
            {"direction": "INBOUND", "body": "Não, obrigado"},
        ],
    }
    decision = _apply_repeated_rejection_policy(_rejection_decision(), second)
    assert decision.reply_text == REJECTION_CLOSE_MESSAGE
    assert decision.followup_needed is False
    assert decision.quick_replies == []

    # Voltou a se interessar no meio: a recusa antiga não conta.
    renewed = {
        "latest_customer_message": "Não quero",
        "history": first["history"] + [
            {"direction": "OUTBOUND", "body": "Entendi! E hoje você já tem internet em casa?"},
            {"direction": "INBOUND", "body": "Tenho, mas quero ver o preço do 800"},
            {"direction": "OUTBOUND", "body": "O 800 Mega sai por R$ 119,90."},
            {"direction": "INBOUND", "body": "Não quero"},
        ],
    }
    decision = _apply_repeated_rejection_policy(_rejection_decision(), renewed)
    assert decision.reply_text != REJECTION_CLOSE_MESSAGE


def test_followup_respeita_intervalo_minimo_de_2h():
    from datetime import timedelta as _td

    from app.services import ai_followups
    from app.timeutils import utcnow as _now

    now = _now()

    class _Db:
        def __init__(self, value):
            self.value = value

        def scalar(self, _stmt):
            return self.value

    hold = ai_followups._followup_spacing_hold(_Db(now - _td(minutes=30)), 1, now)
    assert hold is not None
    assert hold >= now - _td(minutes=30) + _td(hours=2)
    assert ai_followups._followup_spacing_hold(_Db(now - _td(hours=3)), 1, now) is None
    assert ai_followups._followup_spacing_hold(_Db(None), 1, now) is None


def test_followup_do_sdr_para_depois_de_sem_interesse():
    from app.services import ai_followups

    class _Msg:
        def __init__(self, body):
            self.body = body
            self.id = 10

    class _Db:
        def __init__(self, body):
            self.body = body
            self.calls = 0

        def scalar(self, _stmt):
            self.calls += 1
            return _Msg(self.body) if self.calls == 1 else "Qual seu CPF?"

    assert ai_followups._customer_declined(_Db("Não tenho interesse"), 1) is True
    assert ai_followups._customer_declined(_Db("Não, obrigado"), 1) is True
    assert ai_followups._customer_declined(_Db("me remova da lista"), 1) is True
    assert ai_followups._customer_declined(_Db("Não"), 1) is False
    assert ai_followups._customer_declined(_Db("Qual a multa de cancelamento?"), 1) is False


def test_ia_assume_cliente_que_o_vendedor_online_nao_respondeu_em_10_minutos(monkeypatch):
    from datetime import timedelta as _td

    from app.models import LeadQueuePreference
    from app.timeutils import utcnow as _now
    from app.workers.ai_sdr_worker import _takeover_unanswered_human_conversations

    auto = replace(settings, ai_autonomous_enabled=True)
    monkeypatch.setattr("app.services.ai_sdr.settings", auto)
    monkeypatch.setattr("app.services.conversation_engine.settings", auto)
    with TestClient(app):
        db = SessionLocal()
        try:
            for stale_job in db.scalars(select(AIJob).where(AIJob.status == "PENDENTE")):
                stale_job.status = "IGNORADO"
            db.flush()
            agent, queue, human = _create_bound_digital_queue(db, "TAKEOVER")
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id))
            binding.handling_mode = "AI_WHEN_OFFLINE"
            db.commit()
            lead = Lead(
                business_name="Cliente Sem Resposta",
                contact_name="Cliente",
                phone="5555988877622",
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST_AI_TAKEOVER"))
            db.commit()
            conversation, message, _created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-TAKEOVER-1",
                    "senderName": "Cliente",
                    "text": {"message": "Quero saber mais"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert conversation.assigned_user_id == human.id
            assert db.scalar(select(AIJob).where(AIJob.message_id == message.id)) is None

            # Ainda dentro dos 10 minutos: fica com o vendedor.
            binding.updated_at = _now() - _td(hours=1)
            db.commit()
            _takeover_unanswered_human_conversations()
            db.expire_all()
            assert db.scalar(select(AIJob).where(AIJob.message_id == message.id)) is None

            # 15 minutos sem resposta: a IA assume.
            message = db.get(Message, message.id)
            message.created_at = _now() - _td(minutes=15)
            db.commit()
            assert _takeover_unanswered_human_conversations() >= 1
            db.expire_all()
            job = db.scalar(select(AIJob).where(AIJob.message_id == message.id))
            assert job is not None
            assert job.agent_id == agent.id
            assert db.get(type(conversation), conversation.id).assigned_user_id is None
            state = db.scalar(
                select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
            )
            assert state.state == "AI_ACTIVE"
        finally:
            db.rollback()
            db.close()


def test_nao_seco_para_a_black_depois_de_nao_obrigada_encerra_e_sem_followup():
    from app.services import ai_followups
    from app.services.ai_sdr_policies import REJECTION_CLOSE_MESSAGE, _apply_repeated_rejection_policy

    history = [
        {"direction": "OUTBOUND", "body": "Oi! A Sulnet tem novidades para você."},
        {"direction": "INBOUND", "body": "Não obrigada"},
        {"direction": "OUTBOUND", "body": "Antes de te mostrar as opções, você já é cliente Sulnet?"},
        {"direction": "INBOUND", "body": "Sim"},
        {"direction": "OUTBOUND", "body": "Quer conhecer nossa promoção Black da Sulnet?"},
        {"direction": "INBOUND", "body": "Não"},
    ]
    decision = _apply_repeated_rejection_policy(
        _rejection_decision(), {"latest_customer_message": "Não", "history": history}
    )
    assert decision.reply_text == REJECTION_CLOSE_MESSAGE
    assert decision.followup_needed is False

    class _Msg:
        def __init__(self, body, id_=10):
            self.body = body
            self.id = id_

    class _Db:
        def __init__(self, inbound, outbound):
            self.values = [_Msg(inbound), outbound]

        def scalar(self, _stmt):
            return self.values.pop(0)

    assert ai_followups._customer_declined(_Db("Não", "Quer conhecer nossa promoção Black da Sulnet?"), 1) is True
    assert ai_followups._customer_declined(_Db("Não", "A portabilidade será no seu nome? (Sim/Não)"), 1) is False
