from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIFollowupJob,
    Conversation,
    AIQueueBinding,
    Lead,
    LeadQueuePreference,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.ai_followups import (
    _followup_is_repetitive,
    cancel_pending_followups,
    schedule_followup,
)
from app.services.ai_sdr_policy import FTTH_PLANS, followup_spec
from app.services.conversation_engine import apply_conversation_scope, store_received_payload
from app.services.whatsapp_presence import join_whatsapp


def _agent(db):
    return db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital")
    )


def test_650_official_speed_is_650_200():
    plan = next(item for item in FTTH_PLANS if item["code"] == "FTTH_650")
    assert plan["download_mbps"] == 650
    assert plan["upload_mbps"] == 200
    assert plan["price_fidelity"] == 99.90


def test_followup_cadence_starts_2h_3h_3h_and_becomes_monthly_after_step_20():
    delay_1, theme_1 = followup_spec(1)
    delay_2, theme_2 = followup_spec(2)
    delay_3, theme_3 = followup_spec(3)
    delay_4, _theme_4 = followup_spec(4)
    delay_5, _theme_5 = followup_spec(5)
    delay_6, _theme_6 = followup_spec(6)
    delay_20, _theme_20 = followup_spec(20)
    delay_21, theme_21 = followup_spec(21)

    assert delay_1 == timedelta(hours=2)
    assert delay_2 == timedelta(hours=3)
    assert delay_3 == timedelta(hours=3)
    assert "reativação 1" in theme_1
    assert "reativação 2" in theme_2
    assert "reativação 3" in theme_3
    assert delay_4 == timedelta(hours=6)
    assert delay_5 == timedelta(days=1)
    assert delay_6 == timedelta(days=2)
    assert delay_20 == timedelta(days=30)
    assert delay_21 == timedelta(days=30)
    assert "reativação mensal" in theme_21


def test_followup_blocks_router_paraphrases_from_recent_history():
    previous = [
        (
            "Entendi, sua dúvida é sobre quantos roteadores estão incluídos. "
            "Posso confirmar isso antes de avançarmos?"
        ),
        (
            "Sobre os roteadores, ainda preciso confirmar quantos acompanham o plano. "
            "Quer que eu verifique isso?"
        ),
    ]
    candidate = (
        "Entendi que sua dúvida é sobre os roteadores. "
        "Posso confirmar quantos acompanham o plano antes de seguirmos?"
    )
    assert _followup_is_repetitive(candidate, previous) is True


def test_followup_allows_a_genuinely_different_angle():
    previous = [
        "Quantos roteadores são decisivos para você escolher o plano de 650 Mega?"
    ]
    candidate = (
        "Enquanto isso, posso te explicar como funciona a cobertura Wi-Fi dentro da casa "
        "e quando vale a pena usar um equipamento adicional?"
    )
    assert _followup_is_repetitive(candidate, previous) is False


def test_followup_job_is_persistent_and_cancelled_when_context_changes():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _agent(db)
            queue = ServiceQueue(
                name="Fila Followup Teste",
                code="FOLLOWUP-TEST",
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
            lead = Lead(
                business_name="Lead Follow-up",
                contact_name="Cliente",
                phone="5555988899011",
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
                    source="TEST",
                )
            )
            db.commit()

            conversation, message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "FOLLOWUP-IN-1",
                    "senderName": "Cliente",
                    "text": {"message": "Quero saber os planos."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True
            job = schedule_followup(
                db,
                agent_id=agent.id,
                queue_id=queue.id,
                conversation_id=conversation.id,
                source_message_id=message.id,
                step=1,
            )
            db.commit()
            assert job is not None
            assert job.status == "PENDENTE"

            cancelled = cancel_pending_followups(
                db,
                conversation.id,
                reason="CLIENTE_RESPONDEU",
            )
            db.commit()
            assert cancelled == 1
            persisted = db.get(AIFollowupJob, job.id)
            assert persisted.status == "CANCELADO"
        finally:
            db.close()


def test_autonomous_ai_queue_holds_lead_until_handoff(monkeypatch):
    monkeypatch.setattr(
        "app.services.conversation_engine.settings",
        replace(settings, ai_autonomous_enabled=True, ai_sdr_enabled=True),
    )
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _agent(db)
            queue = ServiceQueue(
                name="Fila IA Autônoma Teste",
                code="AI-HOLD-TEST",
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
            seller = User(
                name="Vendedor da Fila",
                email="vendedor-ai-hold@sulnet.local",
                password_hash=hash_password("SenhaForte@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add(seller)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True))
            join_whatsapp(db, seller)

            lead = Lead(
                business_name="Lead IA",
                contact_name="Cliente",
                phone="5555988899012",
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
            db.add(LeadQueuePreference(lead_id=lead.id, queue_id=queue.id, source="TEST"))
            db.commit()

            conversation, _message, created = store_received_payload(
                db,
                {
                    "phone": lead.phone,
                    "messageId": "AI-HOLD-IN-1",
                    "senderName": "Cliente",
                    "text": {"message": "Tenho interesse em internet."},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
            )
            db.commit()
            assert created is True
            assert conversation.assigned_user_id is None
            assert conversation.status == "EM_FILA"
            visible = list(
                db.scalars(
                    apply_conversation_scope(
                        select(Conversation).where(Conversation.id == conversation.id),
                        seller,
                    )
                )
            )
            # Enquanto a IA atende, a conversa não aparece para o vendedor:
            # ela só chega a ele quando o carrossel atribui após o handoff.
            assert visible == []
        finally:
            db.close()
