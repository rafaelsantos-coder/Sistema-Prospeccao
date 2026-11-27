"""Regressao da politica de producao: nenhuma excecao fora da Fila IA."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIConversationState, AIFollowupJob, AIJob, AIQueueBinding, ConversationQueueLink, Message, ServiceQueue
from app.services import ai_queue_scope as scope, ai_sdr
from app.services.ai_followups import process_followup_job
from app.services.ai_queue_coverage import ensure_unanswered_coverage_all_queues
from app.services.ai_reply_delivery import process_next_ai_reply, queue_ai_reply
from app.services.queue_routing import MANUAL_AI_ORIGIN, ai_transfer_queue, bind_conversation_queue, usable_ai_binding
from app.services import zapi_instances
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database
from test_sulia_manual_origin_handoff import _make
from app.routers.conversations import _move_conversation_to_queue


def test_production_blocks_stale_manual_delegation_followups_and_delivery(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, _ = _make(db)
            # Simula pendencias antigas autorizadas fora da fila antes da nova politica.
            monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
            bind_conversation_queue(db, conv.id, human.id, origin=MANUAL_AI_ORIGIN)
            db.add(AIConversationState(conversation_id=conv.id, agent_id=agent.id, state="AI_ACTIVE"))
            conv.assigned_user_id = agent.user_id
            pending = queue_ai_reply(db, conversation=conv, agent=agent, inbound_id=inbound.id, text="Mensagem simulada")
            job = AIJob(queue_id=dedicated.id, agent_id=agent.id, conversation_id=conv.id, message_id=inbound.id, status="PENDENTE")
            followup = AIFollowupJob(queue_id=dedicated.id, agent_id=agent.id, conversation_id=conv.id, source_message_id=inbound.id, status="PENDENTE", due_at=utcnow()-timedelta(minutes=1))
            db.add_all([job, followup, AIQueueBinding(queue_id=human.id, agent_id=agent.id, handling_mode="AI_FIRST", active=True, manual_authorized=True)])
            db.commit()

            # Mesmo uma configuracao legada de escopo apontando para Geral nao autoriza.
            monkeypatch.setenv("AI_DEDICATED_QUEUE_ONLY", "true")
            monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", str(human.id))
            monkeypatch.setattr(zapi_instances, "service_for_conversation", lambda *_a: (_ for _ in ()).throw(AssertionError("envio externo proibido")))
            assert not scope.ai_queue_enabled(db, human.id)
            assert not scope.manual_ai_handoff_authorized(db, conv.id)
            assert not scope.ai_allowed_for_conversation(db, conv.id, fallback_queue_id=dedicated.id)
            assert usable_ai_binding(db, human.id) is None
            assert ai_sdr.enqueue_ai_job(db, conversation=conv, message=inbound) is None
            assert process_next_ai_reply(db)
            assert pending.status == "CANCELED"
            assert process_followup_job(db, followup) == "cancelled"
            followup.status = "PENDENTE"
            result = scope.enforce_exclusive_ai_queue(db)
            db.commit()
            assert result["jobs_cancelled"] == 1
            assert result["followups_cancelled"] == 1
            assert result["delegations_revoked"] == 1
            assert job.status == "CANCELADO"
            assert conv.assigned_user_id is None
            assert db.get(Message, inbound.id) is not None
            link = db.scalar(select(ConversationQueueLink).where(ConversationQueueLink.conversation_id == conv.id))
            assert link.queue_id == human.id
            assert link.origin != MANUAL_AI_ORIGIN
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == human.id))
            assert not binding.active and not binding.manual_authorized
            assert scope.ai_queue_enabled(db, dedicated.id)
            assert ensure_unanswered_coverage_all_queues(db)["enabled"] == []
            assert scope.enforce_exclusive_ai_queue(db)["jobs_cancelled"] == 0
    finally:
        engine.dispose()


def test_dedicated_queue_authorization_and_explicit_transfer_keep_origin(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, origin = _make(db)
            dedicated.code = "LEGACY-AI-CODE"
            db.commit()
            monkeypatch.setenv("AI_DEDICATED_QUEUE_ONLY", "true")
            monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "false")
            assert ai_transfer_queue(db).id == dedicated.id
            assert not scope.ai_allowed_for_conversation(db, conv.id)
            result = _move_conversation_to_queue(db, actor=agent.user, conversation=conv, queue=dedicated, reason="AI")
            assert result["ai_job"] is not None
            assert result["ai_job"].queue_id == dedicated.id
            assert scope.ai_allowed_for_conversation(db, conv.id)
            assert zapi_instances.conversation_instance(db, conv.id).id == origin.id
            assert scope.manual_ai_handoff_authorized(db, conv.id)
            # Ao retornar para uma fila humana, o vinculo atual bloqueia inclusive
            # pendencias da fila IA e nao depende de sua origem.
            bind_conversation_queue(db, conv.id, human.id, origin=MANUAL_AI_ORIGIN)
            db.flush()
            assert not scope.ai_allowed_for_conversation(db, conv.id, fallback_queue_id=dedicated.id)
            assert not scope.manual_ai_handoff_authorized(db, conv.id)
            # Fila IA pausada nao pode ser reativada pelo reconciliador.
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == dedicated.id))
            binding.active = False
            db.flush()
            scope.enforce_exclusive_ai_queue(db)
            assert not binding.active
            assert not scope.ai_queue_enabled(db, dedicated.id)
    finally:
        engine.dispose()


def test_ambiguous_fila_ia_fails_closed(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, _ = _make(db)
            monkeypatch.setenv("AI_DEDICATED_QUEUE_ONLY", "true")
            db.add(ServiceQueue(name="Fila IA", code="DUPLICATE-IA", team="DIGITAL", active=True))
            db.flush()
            assert scope.dedicated_ai_queue(db) is None
            assert not scope.ai_queue_enabled(db, dedicated.id)
            assert ai_transfer_queue(db) is None
    finally:
        engine.dispose()
