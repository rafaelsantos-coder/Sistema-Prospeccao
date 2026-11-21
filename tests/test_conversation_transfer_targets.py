from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIJob,
    AIQueueBinding,
    AIRun,
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.services.conversation_engine import ensure_open_conversation
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance, service_for_conversation
from app.security import hash_password
from app.services.queue_routing import (
    MANUAL_AI_ORIGIN,
    MANUAL_TRANSFER_ORIGIN,
    bind_conversation_queue,
    usable_ai_binding,
)
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": _csrf(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _consultant(db, email: str, name: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    user = User(
        name=name,
        email=email,
        password_hash=hash_password("SenhaFila@2026"),
        role="CONSULTOR",
        team="DIGITAL",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def _queue(db, code: str, name: str) -> ServiceQueue:
    queue = ServiceQueue(
        name=name,
        code=code,
        team="DIGITAL",
        routing_strategy="ROUND_ROBIN",
        active=True,
    )
    db.add(queue)
    db.flush()
    return queue


def _conversation_with_pending_inbound(db, queue: ServiceQueue, phone: str) -> Conversation:
    contact = Contact(phone=phone, name="Cliente Transferência", business_name="Cliente Transferência")
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id,
        team="DIGITAL",
        status="EM_FILA",
        opened_at=utcnow(),
    )
    db.add(conversation)
    db.flush()
    bind_conversation_queue(db, conversation.id, queue.id, origin="TEST")
    db.add(
        Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="INBOUND",
            message_type="TEXT",
            body="Combo fibra + móvel",
            status="RECEIVED",
        )
    )
    db.flush()
    return conversation


def test_offline_seller_of_queue_is_listed_and_can_receive_conversation():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            seller = _consultant(db, "transfer-offline@sulnet.local", "Vendedor Offline Transferência")
            queue = _queue(db, "QUEUE-TRANSFER-OFFLINE", "Fila Transferência Offline")
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True))
            conversation = _conversation_with_pending_inbound(db, queue, "5555988870101")
            db.commit()
            conversation_id = conversation.id
            seller_id = seller.id
        finally:
            db.close()

        _login_admin(client)
        page = client.get(f"/conversations/{conversation_id}")
        assert page.status_code == 200
        assert "Vendedor Offline Transferência · offline" in page.text
        assert "Nenhum outro atendente online" not in page.text

        response = client.post(
            f"/conversations/{conversation_id}/assign",
            data={"csrf_token": _csrf(page.text), "assigned_user_id": str(seller_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            assert db.get(Conversation, conversation_id).assigned_user_id == seller_id
        finally:
            db.close()


def test_pass_to_ai_keeps_original_queue_instance_and_answers_now():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital"))
            assert agent is not None
            human_queue = _queue(db, "QUEUE-TRANSFER-NO-AI", "Fila Sem IA Transferência")
            ai_queue = db.scalar(select(ServiceQueue).where(ServiceQueue.code == "FILA-IA")) or _queue(db, "FILA-IA", "Fila IA")
            binding = db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == ai_queue.id))
            if binding is None:
                binding = AIQueueBinding(queue_id=ai_queue.id)
                db.add(binding)
            binding.agent_id = agent.id
            binding.handling_mode = "AI_WHEN_OFFLINE"
            binding.active = True
            binding.manual_authorized = True
            binding.fallback_to_human = True
            conversation = _conversation_with_pending_inbound(db, human_queue, "5555988870102")
            instance = create_zapi_instance(
                db, name="Origem do teste de transferência", phone_label="Origem",
                base_url="https://example.invalid", instance_id="manual-ai-test",
                instance_token="test-token", client_token="test-client", created_by_id=None,
            )
            bind_queue_instance(db, human_queue.id, instance.id)
            source_queue_id, ai_queue_id, instance_id = human_queue.id, ai_queue.id, instance.id
            message = db.scalar(select(Message).where(Message.conversation_id == conversation.id))
            job = AIJob(agent_id=agent.id, queue_id=human_queue.id, conversation_id=conversation.id,
                        message_id=message.id, status="CONCLUIDO")
            db.add(job)
            db.flush()
            db.add(AIRun(job_id=job.id, agent_id=agent.id, message_id=message.id,
                         conversation_id=conversation.id, execution_mode="SHADOW", status="CONCLUIDO"))
            db.commit()
            conversation_id = conversation.id
        finally:
            db.close()

        _login_admin(client)
        page = client.get(f"/conversations/{conversation_id}")
        assert page.status_code == 200
        assert "Passar para a IA" in page.text

        response = client.post(
            f"/conversations/{conversation_id}/transfer-ai",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            link = db.scalar(
                select(ConversationQueueLink).where(ConversationQueueLink.conversation_id == conversation_id)
            )
            assert link is not None
            assert link.origin == MANUAL_AI_ORIGIN
            assert link.queue_id == source_queue_id
            assert usable_ai_binding(db, ai_queue_id) is not None
            from app.services.ai_queue_scope import ai_allowed_for_conversation, manual_ai_handoff_authorized
            assert manual_ai_handoff_authorized(db, conversation_id)
            assert ai_allowed_for_conversation(db, conversation_id)
            job = db.scalar(select(AIJob).where(AIJob.conversation_id == conversation_id))
            assert job is not None
            assert job.status == "PENDENTE"
            assert job.queue_id == link.queue_id
            assert service_for_conversation(db, conversation_id).instance_id == "manual-ai-test"
            target = link.queue_id
            conversation = db.get(Conversation, conversation_id)
            reopened = ensure_open_conversation(
                db, conversation.contact, queue_id=source_queue_id, zapi_instance_id=instance_id,
            )
            assert reopened.id == conversation_id
            assert link.queue_id == target
            assert link.origin == MANUAL_AI_ORIGIN
            seller = _consultant(
                db, "transfer-back-to-human@sulnet.local", "Vendedor Retomada Humana"
            )
            db.add(ServiceQueueMember(
                queue_id=source_queue_id, user_id=seller.id, active=True,
            ))
            db.commit()
            seller_id = seller.id
        finally:
            db.close()

        # Devolver a um humano encerra somente esta delegacao.
        page = client.get(f"/conversations/{conversation_id}")
        assigned = client.post(
            f"/conversations/{conversation_id}/assign",
            data={
                "csrf_token": _csrf(page.text),
                "assigned_user_id": str(seller_id),
            },
            follow_redirects=False,
        )
        assert assigned.status_code == 303
        with SessionLocal() as db:
            link = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == conversation_id,
            ))
            assert link.queue_id == source_queue_id
            assert link.origin == MANUAL_TRANSFER_ORIGIN
            from app.services.ai_queue_scope import ai_allowed_for_conversation
            assert not ai_allowed_for_conversation(db, conversation_id)
            assert db.get(Conversation, conversation_id).assigned_user_id == seller_id


def test_transfer_to_other_queue_without_ai_leaves_conversation_waiting():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            origin = _queue(db, "QUEUE-TRANSFER-ORIGIN", "Fila Origem Transferência")
            target = _queue(db, "QUEUE-TRANSFER-TARGET", "Fila Destino Transferência")
            conversation = _conversation_with_pending_inbound(db, origin, "5555988870103")
            db.commit()
            conversation_id = conversation.id
            target_id = target.id
        finally:
            db.close()

        _login_admin(client)
        page = client.get(f"/conversations/{conversation_id}")
        assert "Fila Destino Transferência" in page.text

        response = client.post(
            f"/conversations/{conversation_id}/transfer-queue",
            data={"csrf_token": _csrf(page.text), "queue_id": str(target_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            conversation = db.get(Conversation, conversation_id)
            link = db.scalar(
                select(ConversationQueueLink).where(ConversationQueueLink.conversation_id == conversation_id)
            )
            assert link.queue_id == target_id
            assert link.origin == MANUAL_TRANSFER_ORIGIN
            assert conversation.assigned_user_id is None
            assert conversation.status == "EM_FILA"
            assert db.scalar(select(AIJob).where(AIJob.conversation_id == conversation_id)) is None
        finally:
            db.close()
