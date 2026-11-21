"""Delegacao explicita a SulIA sem mover a fila ou usar o numero da Fila IA.

Todos os envios sao simulados em SQLite; nenhum cliente real e acionado.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIJob,
    AIQueueBinding,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    ServiceQueueZApiInstance,
    ZApiInstance,
)
from app.routers.conversations import _move_conversation_to_queue
from app.services import ai_sdr, zapi_instances
from app.services.ai_queue_scope import (
    ai_allowed_for_conversation,
    enforce_exclusive_ai_queue,
    manual_ai_handoff_authorized,
)
from app.services.ai_reply_delivery import process_next_ai_reply, queue_ai_reply
from app.services.conversation_engine import ensure_open_conversation, store_received_payload
from app.services.queue_routing import (
    MANUAL_AI_ORIGIN,
    MANUAL_TRANSFER_ORIGIN,
    bind_conversation_queue,
)
from test_ai_all_queues_coverage import _database, _setup


def _make(db):
    agent_id, dedicated_id, human_id, _ = _setup(db)
    agent = db.get(AIAgentProfile, agent_id)
    agent.active = True
    agent.execution_mode = "AUTO"
    dedicated = db.get(ServiceQueue, dedicated_id)
    dedicated.name, dedicated.code = "Fila IA", "FILA-IA"
    human = db.get(ServiceQueue, human_id)
    human.name, human.code = "Geral Comercial", "GERAL"
    binding = db.scalar(
        select(AIQueueBinding).where(AIQueueBinding.queue_id == dedicated_id)
    )
    binding.active = True
    binding.manual_authorized = True
    binding.handling_mode = "AI_ONLY"

    orig = ZApiInstance(
        name="Numero da Geral", instance_id="origem-humana-teste",
        instance_token_encrypted="fake", webhook_key="manual-ai-teste",
        source_type="DB", active=True,
    )
    db.add(orig)
    db.flush()
    number_link = db.scalar(
        select(ServiceQueueZApiInstance).where(
            ServiceQueueZApiInstance.queue_id == human_id
        )
    )
    number_link.zapi_instance_id = orig.id
    contact = Contact(name="Lead quente", phone="5555999899999")
    db.add(contact)
    db.flush()
    conv = Conversation(
        contact_id=contact.id, source="WHATSAPP", team="DIGITAL",
        status="EM_FILA",
    )
    db.add(conv)
    db.flush()
    bind_conversation_queue(db, conv.id, human_id, origin="INBOUND")
    msg = Message(
        conversation_id=conv.id, contact_id=contact.id,
        direction="INBOUND", message_type="TEXT", body="Quero contratar",
        status="RECEIVED",
    )
    db.add(msg)
    db.flush()
    return agent, dedicated, human, conv, msg, orig


def _delegate(db, agent, human, conv):
    return _move_conversation_to_queue(
        db, actor=agent.user, conversation=conv, queue=human, reason="AI",
    )


def test_delegated_general_keeps_queue_and_uses_original_whatsapp(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    monkeypatch.delenv("AI_EXCLUSIVE_QUEUE_ID", raising=False)
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, origin = _make(db)
            assert not ai_allowed_for_conversation(db, conv.id)
            assert ai_sdr.enqueue_ai_job(db, conversation=conv, message=inbound) is None

            result = _delegate(db, agent, human, conv)
            assert result["ai_job"] is not None
            assert result["ai_job"].status == "PENDENTE"
            assert result["ai_job"].agent_id == agent.id
            assert result["ai_job"].queue_id == human.id
            link = db.scalar(
                select(ConversationQueueLink).where(
                    ConversationQueueLink.conversation_id == conv.id
                )
            )
            assert link.queue_id == human.id
            assert link.origin == MANUAL_AI_ORIGIN
            assert manual_ai_handoff_authorized(db, conv.id)
            assert ai_allowed_for_conversation(db, conv.id)
            assert db.scalar(
                select(AIQueueBinding).where(AIQueueBinding.queue_id == human.id)
            ) is None

            # Outra conversa da MESMA fila continua exclusivamente humana.
            unrelated_contact = Contact(name="Outro cliente", phone="5555988811122")
            db.add(unrelated_contact)
            db.flush()
            unrelated = Conversation(
                contact_id=unrelated_contact.id, source="WHATSAPP",
                team="DIGITAL", status="EM_FILA",
            )
            db.add(unrelated)
            db.flush()
            bind_conversation_queue(db, unrelated.id, human.id, origin="INBOUND")
            assert not ai_allowed_for_conversation(db, unrelated.id)

            monkeypatch.setattr(ai_sdr, "build_ai_context", lambda *_a, **_kw: {})
            monkeypatch.setattr(
                ai_sdr.dispatch_offer, "source_dispatch", lambda *_a, **_kw: None,
            )
            monkeypatch.setattr(ai_sdr, "_contextual_service_reply", lambda *_a, **_kw: None)
            monkeypatch.setattr(ai_sdr, "_try_recall_path", lambda *_a, **_kw: None)
            monkeypatch.setattr(ai_sdr, "_try_fast_purchase_path", lambda *_a, **_kw: "synthetic")
            assert ai_sdr.process_ai_job(db, result["ai_job"]) == "synthetic"
            state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conv.id
                )
            )
            assert state.state == "AI_ACTIVE"
            assert state.agent_id == agent.id
            result["ai_job"].status = "CONCLUIDO"

            output = queue_ai_reply(
                db, conversation=conv, agent=agent, inbound_id=inbound.id,
                text="Vamos seguir com sua contratacao?",
            )
            db.commit()
            sent = []
            monkeypatch.setattr(
                zapi_instances, "service_for_instance",
                lambda instance: SimpleNamespace(
                    instance_id=instance.instance_id,
                    send_text=lambda **kwargs: sent.append(
                        (instance.instance_id, kwargs)
                    ) or {"messageId": "manual-origin-fake-sent"},
                ),
            )
            assert process_next_ai_reply(db)
            assert output.status == "SENT"
            assert output.provider_message_id == "manual-origin-fake-sent"
            assert len(sent) == 1
            assert sent[0][0] == origin.instance_id
            assert "contratacao" in sent[0][1]["message"]

            same = ensure_open_conversation(
                db, conv.contact, queue_id=human.id, zapi_instance_id=origin.id,
            )
            assert same.id == conv.id
            assert link.queue_id == human.id
            assert link.origin == MANUAL_AI_ORIGIN
            assert same.assigned_user_id is None

            # Novo inbound pelo WhatsApp da fila original gera outro AIJob,
            # sem mover o cliente ou atribuir o atendimento a um vendedor.
            next_conv, next_message, created = store_received_payload(
                db,
                {
                    "phone": conv.contact.phone,
                    "messageId": "MANUAL-IA-NEXT-INBOUND-001",
                    "senderName": "Lead quente",
                    "text": {"message": "Pode consultar meu CPF?"},
                    "fromMe": False,
                    "status": "RECEIVED",
                },
                queue_id=human.id,
                zapi_instance_id=origin.id,
            )
            assert created and next_conv.id == conv.id
            next_job = db.scalar(
                select(AIJob).where(AIJob.message_id == next_message.id)
            )
            assert next_job is not None
            assert next_job.status == "PENDENTE"
            assert next_job.agent_id == agent.id
            assert next_job.queue_id == human.id
            assert link.origin == MANUAL_AI_ORIGIN
            assert next_conv.assigned_user_id is None
    finally:
        engine.dispose()


def test_delegation_does_not_enable_general_and_can_return_to_human(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, _ = _make(db)
            result = _delegate(db, agent, human, conv)
            assert result["ai_job"] is not None
            assert manual_ai_handoff_authorized(db, conv.id)
            db.add(AIConversationState(
                conversation_id=conv.id, agent_id=agent.id, state="AI_ACTIVE",
            ))
            pending = queue_ai_reply(
                db, conversation=conv, agent=agent, inbound_id=inbound.id,
                text="Qual o CPF?",
            )
            db.flush()
            bind_conversation_queue(
                db, conv.id, human.id, origin=MANUAL_TRANSFER_ORIGIN,
            )
            assert not manual_ai_handoff_authorized(db, conv.id)
            assert not ai_allowed_for_conversation(db, conv.id)
            assert ai_sdr.binding_for_conversation(db, conv.id) is None
            db.commit()
            assert process_next_ai_reply(db)
            assert pending.status == "CANCELED"
    finally:
        engine.dispose()


def test_scope_repairs_preserve_only_explicit_manual_handoff(monkeypatch):
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, _ = _make(db)
            result = _delegate(db, agent, human, conv)
            original_job = result["ai_job"]
            db.add(AIConversationState(
                conversation_id=conv.id, agent_id=agent.id, state="AI_ACTIVE",
            ))
            db.flush()
            # A restricao antiga de revogacao automatica da Geral foi removida.
            # Uma delegacao manual continua valida mesmo sem vinculo automatico.
            assert original_job.status == "PENDENTE"
            assert ai_allowed_for_conversation(db, conv.id)

            # Sob escopo legado exclusivo, a delegacao explicita ainda persiste.
            # As demais conversas sem vinculo aprovado permanecem humanas.
            monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "false")
            monkeypatch.setenv("AI_EXCLUSIVE_QUEUE_ID", str(dedicated.id))
            enforce_exclusive_ai_queue(db)
            db.flush()
            assert original_job.status == "PENDENTE"
            assert ai_allowed_for_conversation(db, conv.id)
            assert conv.status == "EM_FILA"
            link = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == conv.id,
            ))
            assert link.queue_id == human.id
    finally:
        engine.dispose()


def test_legacy_manual_transfer_to_fila_ia_keeps_saved_origin_number(monkeypatch):
    """Conversas movidas antes da correcao nao voltam para ZAPI desconectada."""
    monkeypatch.setenv("AI_QUEUE_MANUAL_CONTROL", "true")
    engine = _database()
    try:
        with Session(engine) as db:
            agent, dedicated, human, conv, inbound, origin = _make(db)
            # Formato historico: link mudou para Fila IA, mas o numero
            # original continuou registrado na conversa.
            zapi_instances.bind_conversation_instance(db, conv.id, origin.id)
            bind_conversation_queue(
                db, conv.id, dedicated.id, origin=MANUAL_AI_ORIGIN,
            )
            monkeypatch.setattr(
                zapi_instances, "service_for_instance",
                lambda instance: SimpleNamespace(instance_id=instance.instance_id),
            )
            service = zapi_instances.service_for_conversation(db, conv.id)
            assert service.instance_id == origin.instance_id
            assert service.instance_id != "coverage-unique-test"
            current = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == conv.id,
            ))
            assert current.queue_id == dedicated.id
            assert current.origin == MANUAL_AI_ORIGIN
    finally:
        engine.dispose()
