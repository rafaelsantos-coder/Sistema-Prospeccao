"""Cobertura de todas as filas e recuperação de não lidas sem mensagens duplicadas."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIQueueBinding,
    AuditLog,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    ServiceQueueZApiInstance,
    User,
    ZApiInstance,
)
from app.services.ai_queue_coverage import (
    COVERAGE_ACTION,
    ensure_unanswered_coverage_all_queues,
)
from app.timeutils import utcnow
from app.workers import ai_sdr_worker


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _setup(db: Session):
    user = User(
        name="SDR IA teste",
        email="sdr-coverage-test@sulnet.local",
        password_hash="fake",
        role="AI_SDR",
        team="DIGITAL",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    agent = AIAgentProfile(
        user_id=user.id,
        name="SulIA",
        slug="sulia-sdr-digital",
        role_type="SDR",
        execution_mode="AUTO",
        active=True,
    )
    db.add(agent)
    number = ZApiInstance(
        name="WhatsApp para testes",
        instance_id="coverage-unique-test",
        instance_token_encrypted="encrypted-placeholder",
        webhook_key="coverage-unique-test",
        source_type="DB",
        active=True,
    )
    db.add(number)
    db.flush()
    qa = ServiceQueue(name="Individual", code="COVER-INDIVIDUAL", team="DIGITAL", active=True)
    qb = ServiceQueue(name="Natine", code="COVER-NATINE", team="DIGITAL", active=True)
    qc = ServiceQueue(name="Sem número", code="COVER-NUMBERLESS", team="DIGITAL", active=True)
    db.add_all([qa, qb, qc])
    db.flush()
    db.add_all([
        ServiceQueueZApiInstance(queue_id=qa.id, zapi_instance_id=number.id),
        ServiceQueueZApiInstance(queue_id=qb.id, zapi_instance_id=number.id),
        AIQueueBinding(
            queue_id=qa.id,
            agent_id=agent.id,
            handling_mode="HUMAN",
            active=True,
        ),
    ])
    db.commit()
    return agent.id, qa.id, qb.id, qc.id


def test_all_whatsapp_queues_assume_ai_once_and_keep_manual_changes():
    engine = _database()
    try:
        with Session(engine) as db:
            _, qa, qb, qc = _setup(db)
            result = ensure_unanswered_coverage_all_queues(db)
            assert set(result["enabled"]) == {qa, qb}
            assert result["without_whatsapp"] == [qc]
            for queue_id in (qa, qb):
                binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id))
                assert binding is not None
                assert binding.active
                assert binding.handling_mode == "AI_WHEN_HUMAN_IDLE"
            assert db.scalar(
                select(AuditLog.id).where(AuditLog.action == COVERAGE_ACTION)
            )
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == qa))
            binding.active = False
            db.commit()
            second = ensure_unanswered_coverage_all_queues(db)
            assert second["already_applied"] is True
            db.refresh(binding)
            assert binding.active is False, "o gestor pode pausar a IA depois da configuração"
    finally:
        engine.dispose()


def test_unanswered_human_chat_is_requeued_once_after_coverage(monkeypatch):
    engine = _database()
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            agent_id, qa, _, _ = _setup(db)
            ensure_unanswered_coverage_all_queues(db)
            contact = Contact(phone="5555999991234", name="Cliente aguardando")
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id, team="DIGITAL", source="WHATSAPP",
                status="EM_FILA", last_message_at=utcnow() - timedelta(minutes=18),
            )
            db.add(conversation)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=conversation.id, queue_id=qa, origin="INBOUND",
            ))
            message = Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND", message_type="TEXT",
                body="Quero contratar a internet", status="RECEIVED",
                created_at=utcnow() - timedelta(minutes=18),
            )
            db.add(message)
            db.commit()
            conv_id = conversation.id
            msg_id = message.id
        monkeypatch.setattr(ai_sdr_worker, "SessionLocal", factory)
        monkeypatch.setattr(ai_sdr_worker, "_real_mode_cutoff", lambda: None)
        count = ai_sdr_worker._takeover_unanswered_human_conversations()
        assert count == 1
        with factory() as db:
            jobs = list(db.scalars(select(AIJob).where(AIJob.message_id == msg_id)))
            assert len(jobs) == 1
            assert jobs[0].status == "PENDENTE"
            assert jobs[0].agent_id == agent_id
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conv_id
                )
            )
            assert state.state == "AI_ACTIVE"
        assert ai_sdr_worker._takeover_unanswered_human_conversations() == 0
    finally:
        engine.dispose()


def test_reuses_ignored_job_instead_of_creating_duplicate(monkeypatch):
    engine = _database()
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            agent_id, qa, _, _ = _setup(db)
            ensure_unanswered_coverage_all_queues(db)
            contact = Contact(phone="5555999998888")
            db.add(contact)
            db.flush()
            conv = Conversation(contact_id=contact.id, source="WHATSAPP", status="EM_FILA")
            db.add(conv)
            db.flush()
            db.add(ConversationQueueLink(conversation_id=conv.id, queue_id=qa, origin="INBOUND"))
            msg = Message(
                conversation_id=conv.id, contact_id=contact.id,
                direction="INBOUND", message_type="TEXT",
                body="Quero informações", status="RECEIVED",
                created_at=utcnow() - timedelta(minutes=30),
            )
            db.add(msg)
            db.flush()
            db.add(AIJob(
                conversation_id=conv.id,
                message_id=msg.id,
                queue_id=qa,
                agent_id=agent_id,
                status="IGNORADO",
                error="Antes tinha vendedor online",
            ))
            db.commit()
            msg_id = msg.id
        monkeypatch.setattr(ai_sdr_worker, "SessionLocal", factory)
        monkeypatch.setattr(ai_sdr_worker, "_real_mode_cutoff", lambda: None)
        assert ai_sdr_worker._takeover_unanswered_human_conversations() == 1
        with factory() as db:
            jobs = list(db.scalars(select(AIJob).where(AIJob.message_id == msg_id)))
            assert len(jobs) == 1
            assert jobs[0].status == "PENDENTE"
            assert jobs[0].error == "AI_OFFLINE_RECOVERY"
    finally:
        engine.dispose()
