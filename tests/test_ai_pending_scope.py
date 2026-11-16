"""Regressão: mutirão de 16/11 não pode reabrir mensagens anteriores às 18h."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import AIJob, Contact, Conversation, ConversationQueueLink, Message
from app.services.ai_pending_scope import (
    UNREAD_RECOVERY_SINCE_UTC,
    in_unread_recovery_window,
)
from app.services import ops_alerts
from app.workers import ai_sdr_worker
from test_ai_all_queues_coverage import _database, _setup
from app.services.ai_queue_coverage import ensure_unanswered_coverage_all_queues


TODAY_NOW_UTC = datetime(2026, 11, 16, 23, 0)
BEFORE_18H = datetime(2026, 11, 16, 20, 59, 59)
AFTER_18H = datetime(2026, 11, 16, 21, 1, 0)


def test_recorte_corresponde_a_18h_brasilia_16_nov():
    assert UNREAD_RECOVERY_SINCE_UTC == datetime(2026, 11, 16, 21, 0)
    assert not in_unread_recovery_window(BEFORE_18H, now=TODAY_NOW_UTC)
    assert in_unread_recovery_window(AFTER_18H, now=TODAY_NOW_UTC)
    assert not in_unread_recovery_window(
        datetime(2026, 11, 17, 0, 0), now=TODAY_NOW_UTC
    )
    aware = AFTER_18H.replace(tzinfo=timezone.utc)
    assert in_unread_recovery_window(aware, now=TODAY_NOW_UTC)


def _pending(db, queue_id: int, phone: str, received: datetime):
    contact = Contact(phone=phone, name="Pessoa aguardando")
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
        status="EM_FILA", updated_at=received,
    )
    db.add(conversation)
    db.flush()
    db.add(ConversationQueueLink(
        conversation_id=conversation.id, queue_id=queue_id, origin="INBOUND"
    ))
    message = Message(
        conversation_id=conversation.id, contact_id=contact.id,
        direction="INBOUND", message_type="TEXT",
        body="Olá, quero saber mais sobre os planos de internet",
        status="RECEIVED", created_at=received,
    )
    db.add(message)
    db.flush()
    return conversation.id, message.id


def test_mutirao_ignora_ontem_e_17h59_e_pega_apenas_18h_em_diante(monkeypatch):
    engine = _database()
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            agent_id, qa, _, _ = _setup(db)
            ensure_unanswered_coverage_all_queues(db)
            old_yesterday = _pending(
                db, qa, "5555999999701", datetime(2026, 11, 15, 22, 0)
            )
            old_today = _pending(db, qa, "5555999999702", BEFORE_18H)
            new_today = _pending(db, qa, "5555999999703", AFTER_18H)
            db.commit()

        monkeypatch.setattr(ai_sdr_worker, "SessionLocal", factory)
        monkeypatch.setattr(ai_sdr_worker, "utcnow", lambda: TODAY_NOW_UTC)
        monkeypatch.setattr(ai_sdr_worker, "_real_mode_cutoff", lambda: None)
        assert ai_sdr_worker._takeover_unanswered_human_conversations() == 1
        assert ai_sdr_worker._takeover_unanswered_human_conversations() == 0
        with factory() as db:
            for conv_id, message_id in (old_yesterday, old_today):
                assert db.scalar(
                    select(AIJob.id).where(AIJob.message_id == message_id)
                ) is None
            jobs = list(db.scalars(
                select(AIJob).where(AIJob.message_id == new_today[1])
            ))
            assert len(jobs) == 1
            assert jobs[0].agent_id == agent_id
    finally:
        engine.dispose()


def test_alerta_nao_lida_somente_mensagem_recebida_desde_18h(monkeypatch):
    engine = _database()
    try:
        with sessionmaker(bind=engine)() as db:
            _, qa, _, _ = _setup(db)
            ensure_unanswered_coverage_all_queues(db)
            old_today = _pending(db, qa, "5555999999711", BEFORE_18H)
            new_today = _pending(db, qa, "5555999999712", AFTER_18H)
            db.commit()
            monkeypatch.setattr(ops_alerts, "utcnow", lambda: TODAY_NOW_UTC)
            waiting = ops_alerts.unanswered_customers(db)
            ids = {row["conversation_id"] for row in waiting}
            assert new_today[0] in ids
            assert old_today[0] not in ids
    finally:
        engine.dispose()
