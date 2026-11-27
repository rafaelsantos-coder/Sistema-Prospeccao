"""Nova decisao comercial: SulIA responde tambem na Geral Comercial autorizada.

A fila IA continua ativa e humanos que ja atenderam nao sao interrompidos.
Nenhum teste dispara WhatsApp ou consulta CRM.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile, AIConversationState, AIQueueBinding, AIReplyDelivery, Contact, Conversation,
    ConversationQueueLink, Message, ServiceQueue, User,
)
from app.services.ai_queue_scope import (
    ai_allowed_for_conversation, ai_queue_enabled, is_general_commercial_queue,
)
from app.services import ai_sdr
from app.services.ai_reply_delivery import queue_ai_reply
from app.services.ai_sdr import binding_for_conversation, enqueue_ai_job, human_is_attending, queue_ai_binding
from app.services.queue_routing import MANUAL_AI_ORIGIN, bind_conversation_queue, usable_ai_binding
from test_ai_all_queues_coverage import _database, _setup


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0033_enable_general_commercial_sulia.py"
    spec = importlib.util.spec_from_file_location("migration_0033_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare(db: Session):
    agent_id, ia_queue_id, other_queue_id, _ = _setup(db)
    ia_queue = db.get(ServiceQueue, ia_queue_id)
    ia_queue.name = "Fila IA"
    ia_queue.code = "FILA-IA"
    ia_binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == ia_queue_id))
    ia_binding.handling_mode = "AI_ONLY"
    ia_binding.active = True
    ia_binding.manual_authorized = True
    general = ServiceQueue(
        name="Geral Comercial", code="GERAL-COMERCIAL", team="DIGITAL", active=True,
    )
    db.add(general)
    db.commit()
    return agent_id, ia_queue_id, other_queue_id, general.id


def _apply(engine, monkeypatch):
    migration = _migration()
    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        migration.upgrade()  # idempotente


def _inbound(db, queue_id, *, name="Cliente", assigned_user_id=None):
    contact = Contact(name=name, phone=f"555599999{queue_id:04d}")
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
        status="EM_FILA", assigned_user_id=assigned_user_id,
    )
    db.add(conversation)
    db.flush()
    db.add(ConversationQueueLink(conversation_id=conversation.id, queue_id=queue_id, origin="INBOUND"))
    inbound = Message(
        contact_id=contact.id, conversation_id=conversation.id,
        direction="INBOUND", message_type="TEXT", status="RECEIVED",
        body="Quero contratar a internet",
    )
    db.add(inbound)
    db.flush()
    return conversation, inbound


def test_migration_autoriza_geral_e_preserva_fila_ia(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", "2")  # legado nao desautoriza no modo manual
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, ia_queue_id, other_id, general_id = _prepare(db)
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            assert is_general_commercial_queue(db.get(ServiceQueue, general_id))
            assert is_general_commercial_queue(SimpleNamespace(name="Comercial Geral", code="X"))
            general_binding = queue_ai_binding(db, general_id)
            assert general_binding and general_binding.agent_id == agent_id
            assert general_binding.handling_mode == "AI_FIRST"
            assert general_binding.manual_authorized and general_binding.active
            assert general_binding.fallback_to_human
            assert ai_queue_enabled(db, general_id)
            assert usable_ai_binding(db, general_id) is not None
            assert ai_queue_enabled(db, ia_queue_id)
            assert queue_ai_binding(db, ia_queue_id).handling_mode == "AI_ONLY"
            assert not ai_queue_enabled(db, other_id)
            assert queue_ai_binding(db, other_id) is None

            conversation, inbound = _inbound(db, general_id)
            assert ai_allowed_for_conversation(db, conversation.id)
            assert binding_for_conversation(db, conversation.id).agent_id == agent_id
            job = enqueue_ai_job(db, conversation=conversation, message=inbound)
            assert job is not None and job.status == "PENDENTE" and job.queue_id == general_id
            # Garante AUTO real, sem cair em SHADOW por nao ser a Fila IA.
            monkeypatch.setattr(ai_sdr, "build_ai_context", lambda *_a, **_kw: {})
            monkeypatch.setattr(ai_sdr.dispatch_offer, "source_dispatch", lambda *_a, **_kw: None)
            monkeypatch.setattr(ai_sdr, "_contextual_service_reply", lambda *_a, **_kw: None)
            monkeypatch.setattr(ai_sdr, "_try_recall_path", lambda *_a, **_kw: None)
            monkeypatch.setattr(ai_sdr, "_try_fast_purchase_path", lambda *_a, **_kw: "synthetic")
            assert ai_sdr.process_ai_job(db, job) == "synthetic"
            owner = db.scalar(select(AIConversationState).where(
                AIConversationState.conversation_id == conversation.id,
            ))
            assert owner is not None and owner.state == "AI_ACTIVE"
            assert owner.agent_id == agent_id
            response = queue_ai_reply(
                db, conversation=conversation, agent=db.get(AIAgentProfile, agent_id),
                inbound_id=inbound.id, text="Olá, vamos continuar a contratação?",
            )
            assert response.status == "PENDING"
            assert db.scalar(select(AIReplyDelivery.id).where(
                AIReplyDelivery.inbound_message_id == inbound.id,
            ))
            db.commit()
    finally:
        engine.dispose()


def test_humano_que_ja_respondeu_na_geral_tem_prioridade(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            _, _, _, general_id = _prepare(db)
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            human = User(
                name="Vendedor", email="human-queue-test@sulnet.local",
                password_hash="fake", role="CONSULTOR", team="DIGITAL",
                active=True, must_change_password=False,
            )
            db.add(human)
            db.flush()
            conversation, inbound = _inbound(db, general_id, assigned_user_id=human.id)
            db.add(Message(
                contact_id=conversation.contact_id, conversation_id=conversation.id,
                direction="OUTBOUND", message_type="TEXT", status="SENT",
                sender_user_id=human.id, body="Estou cuidando da sua contratação.",
            ))
            db.flush()
            assert human_is_attending(db, conversation)
            assert enqueue_ai_job(db, conversation=conversation, message=inbound) is None
    finally:
        engine.dispose()


def test_desativacao_gerencial_volta_a_bloquear_respostas_na_geral(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, ia_queue_id, _, general_id = _prepare(db)
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == general_id))
            binding.manual_authorized = False
            db.flush()
            assert not ai_queue_enabled(db, general_id)
            assert ai_queue_enabled(db, ia_queue_id)
            conversation, inbound = _inbound(db, general_id)
            assert not ai_allowed_for_conversation(db, conversation.id)
            assert enqueue_ai_job(db, conversation=conversation, message=inbound) is None
            with pytest.raises(ValueError, match="nao autorizada"):
                queue_ai_reply(
                    db, conversation=conversation, agent=db.get(AIAgentProfile, agent_id),
                    inbound_id=inbound.id, text="Resposta nao permitida",
                )
    finally:
        engine.dispose()


def test_transferencia_manual_continua_funcionando_sem_mudar_numero(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            _, ia_queue_id, _, general_id = _prepare(db)
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == general_id))
            binding.active = False
            db.flush()
            conversation, inbound = _inbound(db, general_id)
            assert not ai_allowed_for_conversation(db, conversation.id)
            bind_conversation_queue(db, conversation.id, general_id, origin=MANUAL_AI_ORIGIN)
            assert ai_allowed_for_conversation(db, conversation.id)
            assert binding_for_conversation(db, conversation.id).queue_id == ia_queue_id
            assert enqueue_ai_job(db, conversation=conversation, message=inbound) is not None
    finally:
        engine.dispose()


def test_migration_sem_fila_nao_cria_fila_nem_autorizacao(monkeypatch):
    engine = _database()
    try:
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            assert not list(db.scalars(select(AIQueueBinding)))
    finally:
        engine.dispose()


def test_human_message_without_assignment_blocks_general_recovery(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            _, _, _, general_id = _prepare(db)
        _apply(engine, monkeypatch)
        with Session(engine) as db:
            human = User(
                name="Vendedor", email="unassigned-human-test@sulnet.local",
                password_hash="fake", role="CONSULTOR", team="DIGITAL",
                active=True, must_change_password=False,
            )
            db.add(human)
            db.flush()
            conversation, inbound = _inbound(db, general_id)
            db.add(Message(
                contact_id=conversation.contact_id, conversation_id=conversation.id,
                direction="OUTBOUND", message_type="TEXT", status="SENT",
                sender_user_id=human.id, body="O senhor é de qual cidade?",
            ))
            db.flush()
            assert not ai_allowed_for_conversation(db, conversation.id)
            assert enqueue_ai_job(db, conversation=conversation, message=inbound, takeover=True) is None
    finally:
        engine.dispose()
