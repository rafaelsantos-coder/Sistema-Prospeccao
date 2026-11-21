"""Recuperacao de job falho deve respeitar delegacao manual da SulIA.

SQLite temporario, sem WhatsApp real e sem CRM real.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from app.models import AIConversationState, AIJob, ConversationQueueLink
from app.services import ai_sdr
from app.timeutils import utcnow
from app.workers import ai_sdr_worker
from test_ai_all_queues_coverage import _database
from test_sulia_manual_origin_handoff import _delegate, _make


def test_recovers_failed_ai_job_after_manual_handoff_even_with_prior_human_reply(
    monkeypatch,
):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            agent, _, human, conversation, inbound, _ = _make(db)
            result = _delegate(db, agent, human, conversation)
            job = result["ai_job"]
            assert job is not None
            db.add(AIConversationState(
                conversation_id=conversation.id, agent_id=agent.id,
                state="AI_ACTIVE",
            ))
            inbound.created_at = utcnow() - timedelta(minutes=5)
            job.status = "FALHOU"
            job.attempts = 3
            job.error = "SDR perdeu a propriedade da conversa: NEW"
            db.commit()
            job_id = job.id
            conversation_id = conversation.id
            original_queue_id = human.id

        monkeypatch.setattr(ai_sdr_worker, "SessionLocal", factory)
        monkeypatch.setattr(ai_sdr_worker, "_real_mode_cutoff", lambda: None)
        # Mensagem manual ANTERIOR nao representa atendente atual; a
        # delegacao explicita deve continuar recuperavel.
        monkeypatch.setattr(ai_sdr, "human_stepped_in", lambda *_a, **_k: True)
        monkeypatch.setattr(ai_sdr, "human_is_attending", lambda *_a, **_k: False)
        assert ai_sdr_worker._answer_stalled_conversations() == 1

        with factory() as db:
            recovered = db.get(AIJob, job_id)
            assert recovered.status == "PENDENTE"
            assert recovered.attempts == 0
            assert "[retomado pela supervisão]" in recovered.error
            assert recovered.queue_id == original_queue_id
            link = db.query(ConversationQueueLink).filter_by(
                conversation_id=conversation_id,
            ).one()
            assert link.origin == "MANUAL_AI"
            assert link.queue_id == original_queue_id

        # O job em PENDENTE nao pode ser recuperado de novo em loop.
        assert ai_sdr_worker._answer_stalled_conversations() == 0
    finally:
        engine.dispose()
