"""Resgate de mensagens reais sem resposta; testes locais sem envios externos."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIConversationState, AIJob, AIQueueBinding,
    AuditLog, Contact, Conversation, ConversationQueueLink,
    Message, ServiceQueue, User,
)
from app.services.ai_unanswered_rescue import (
    AUDIT_ACTION, _defers_or_declines, recover_reported_unanswered,
)
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database, _setup


def _ready(db):
    agent_id, general_id, ia_id, _ = _setup(db)
    general = db.get(ServiceQueue, general_id)
    general.name = "Geral Comercial"
    general.code = "GERAL"
    ia = db.get(ServiceQueue, ia_id)
    ia.name = "Fila IA"
    ia.code = "FILA-IA"
    general_binding = db.scalar(select(AIQueueBinding).where(
        AIQueueBinding.queue_id == general_id
    ))
    general_binding.agent_id = agent_id
    general_binding.active = True
    general_binding.manual_authorized = True
    general_binding.handling_mode = "AI_FIRST"
    db.add(AIQueueBinding(
        queue_id=ia_id, agent_id=agent_id, active=True,
        manual_authorized=True, handling_mode="AI_ONLY",
    ))
    agent = db.get(AIAgentProfile, agent_id)
    agent.active = True
    agent.execution_mode = "AUTO"
    db.flush()
    return agent_id, general_id, ia_id


def _customer(db, queue_id, *, body="Quero o combo", assigned_user_id=None):
    contact = Contact(
        name="Cliente teste", phone=f"555599000{queue_id:04d}{db.scalar(select(Conversation.id).limit(1)) or 0:02d}",
    )
    db.add(contact)
    db.flush()
    conv = Conversation(
        contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
        status="EM_ATENDIMENTO", assigned_user_id=assigned_user_id,
    )
    db.add(conv)
    db.flush()
    db.add(ConversationQueueLink(
        conversation_id=conv.id, queue_id=queue_id, origin="INBOUND",
    ))
    inbound = Message(
        conversation_id=conv.id, contact_id=contact.id,
        direction="INBOUND", message_type="TEXT", status="RECEIVED",
        body=body, created_at=utcnow() - timedelta(minutes=30),
    )
    db.add(inbound)
    db.flush()
    return conv, inbound


def test_recovers_general_and_ia_exactly_once(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    try:
        with Session(engine) as db:
            _, general, dedicated = _ready(db)
            first, message1 = _customer(db, general)
            second, message2 = _customer(db, dedicated)
            db.commit()
            result = recover_reported_unanswered(
                db, conversation_ids=(first.id, second.id),
            )
            assert result == {"enqueued": 2}
            db.commit()
            jobs = list(db.scalars(select(AIJob).order_by(AIJob.id)))
            assert len(jobs) == 2
            assert {job.conversation_id for job in jobs} == {first.id, second.id}
            assert all(job.status == "PENDENTE" for job in jobs)
            assert len(list(db.scalars(
                select(AuditLog).where(AuditLog.action == AUDIT_ACTION)
            ))) == 2

            repeated = recover_reported_unanswered(
                db, conversation_ids=(first.id, second.id),
            )
            assert repeated == {"already_rescued": 2}
            assert len(list(db.scalars(select(AIJob)))) == 2
            assert {job.message_id for job in jobs} == {message1.id, message2.id}
    finally:
        engine.dispose()


def test_stale_human_assignment_can_be_reclaimed_only_if_latest_unanswered(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, general, _ = _ready(db)
            human = User(
                name="Vendedor antigo", email="human-rescue-test@sulnet.local",
                password_hash="fake", role="CONSULTOR",
                team="DIGITAL", active=True, must_change_password=False,
            )
            db.add(human)
            db.flush()
            conv, inbound = _customer(db, general, assigned_user_id=human.id)
            manual = Message(
                conversation_id=conv.id, contact_id=conv.contact_id,
                direction="OUTBOUND", message_type="TEXT", status="SENT",
                sender_user_id=human.id, body="Em que posso ajudar?",
                created_at=utcnow() - timedelta(minutes=45),
            )
            db.add(manual)
            db.flush()
            assert manual.id > inbound.id
            # O teste de pendencia usa a mensagem com id mais alto, portanto
            # criar a mensagem nova depois da mensagem manual anterior.
            db.delete(inbound)
            db.flush()
            new_inbound = Message(
                conversation_id=conv.id, contact_id=conv.contact_id,
                direction="INBOUND", message_type="TEXT", status="RECEIVED",
                body="Quero contratar", created_at=utcnow() - timedelta(minutes=30),
            )
            db.add(new_inbound)
            db.flush()
            db.commit()
            result = recover_reported_unanswered(db, conversation_ids=(conv.id,))
            assert result == {"enqueued": 1}
            assert conv.assigned_user_id == db.get(AIAgentProfile, agent_id).user_id
            assert db.scalar(select(AIJob.id).where(AIJob.message_id == new_inbound.id))
            db.commit()
    finally:
        engine.dispose()


def test_does_not_send_after_reply_refusal_or_optout(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, general, _ = _ready(db)
            answered, msg1 = _customer(db, general)
            refused, msg2 = _customer(db, general, body="Agora não")
            opted, msg3 = _customer(db, general)
            db.add(AIConversationState(
                conversation_id=opted.id, agent_id=agent_id, state="OPT_OUT",
            ))
            db.add(Message(
                conversation_id=answered.id, contact_id=answered.contact_id,
                direction="OUTBOUND", message_type="TEXT",
                status="SENT", body="Pode enviar o CPF?",
                sender_user_id=db.get(AIAgentProfile, agent_id).user_id,
            ))
            db.commit()
            result = recover_reported_unanswered(
                db, conversation_ids=(answered.id, refused.id, opted.id),
            )
            assert result.get("not_waiting") == 2
            assert result.get("declined") == 1
            assert not list(db.scalars(select(AIJob)))
    finally:
        engine.dispose()


def test_decline_guard_preserves_non_refusals():
    assert _defers_or_declines("Agora não, obrigado")
    assert _defers_or_declines("Não me envie mensagens")
    assert not _defers_or_declines("Quero contratar agora")
    assert not _defers_or_declines("Tenho interesse")
