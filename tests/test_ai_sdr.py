from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

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
)
from app.security import hash_password
from app.services.ai_sdr import AIDecision, QualificationData, claim_ai_job, process_ai_job
from app.services.conversation_engine import store_received_payload
from app.services.seed import seed_database
from app.services.whatsapp_presence import join_whatsapp


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
