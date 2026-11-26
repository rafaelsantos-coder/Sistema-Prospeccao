"""Somente vendedores com vinculo ativo podem acessar a Fila Digital.

Regra exclusiva da Fila Digital; nao altera o perfil SUPERVISOR nem outras filas.
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Contact,
    Conversation,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.conversation_engine import apply_conversation_scope
from app.services.whatsapp_presence import join_whatsapp


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_digital_requires_active_membership_only_for_consultant():
    suffix = uuid4().hex[:8]
    password = "SenhaDigital@2026"
    phone_suffix = "".join(str(int(ch, 16) % 10) for ch in suffix)

    with TestClient(app) as client:
        with SessionLocal() as db:
            seller = User(
                name="Consultor Digital sem vinculo",
                email=f"digital-acl-{suffix}@sulnet.local",
                password_hash=hash_password(password),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            supervisor = User(
                name="Supervisor sem alteracao de perfil",
                email=f"digital-super-{suffix}@sulnet.local",
                password_hash=hash_password(password),
                role="SUPERVISOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            digital = ServiceQueue(
                name="Fila Digital",
                code=f"ACL-DIGITAL-{suffix}",
                team="DIGITAL",
                active=True,
                routing_strategy="MANUAL",
            )
            other = ServiceQueue(
                name="Geral Comercial ACL",
                code=f"ACL-GERAL-{suffix}",
                team="DIGITAL",
                active=True,
                routing_strategy="MANUAL",
            )
            db.add_all([seller, supervisor, digital, other])
            db.flush()
            contact_digital = Contact(phone=f"55559{phone_suffix}", name="Cliente Digital ACL")
            contact_other = Contact(phone=f"55558{phone_suffix}", name="Cliente Geral ACL")
            db.add_all([contact_digital, contact_other])
            db.flush()

            digital_chat = Conversation(
                contact_id=contact_digital.id,
                assigned_user_id=seller.id,
                team="DIGITAL",
                source="WHATSAPP",
                status="EM_ATENDIMENTO",
            )
            general_chat = Conversation(
                contact_id=contact_other.id,
                assigned_user_id=seller.id,
                team="DIGITAL",
                source="WHATSAPP",
                status="EM_ATENDIMENTO",
            )
            db.add_all([digital_chat, general_chat])
            db.flush()
            db.add_all([
                ConversationQueueLink(
                    conversation_id=digital_chat.id, queue_id=digital.id,
                    origin="INBOUND",
                ),
                ConversationQueueLink(
                    conversation_id=general_chat.id, queue_id=other.id,
                    origin="INBOUND",
                ),
                Message(
                    conversation_id=digital_chat.id,
                    contact_id=contact_digital.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body=f"SEGREDO_DIGITAL_{suffix}",
                    status="RECEIVED",
                ),
                Message(
                    conversation_id=general_chat.id,
                    contact_id=contact_other.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body=f"GERAL_PERMITIDA_{suffix}",
                    status="RECEIVED",
                ),
            ])
            join_whatsapp(db, seller)
            db.commit()
            seller_id, supervisor_id = seller.id, supervisor.id
            digital_id, general_id, digital_queue_id = (
                digital_chat.id, general_chat.id, digital.id
            )
            seller_email = seller.email

        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": seller_email,
                "password": password,
                "csrf_token": _csrf(login_page.text),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = _csrf(client.get("/dashboard").text)

        # Mesmo atribuido ao vendedor e com team=DIGITAL, sem vinculo nao entra.
        blocked = client.get(f"/conversations/{digital_id}", follow_redirects=False)
        assert blocked.status_code == 303
        blocked_api = client.get(f"/api/conversations/{digital_id}/messages?after_id=0")
        assert blocked_api.status_code == 404
        blocked_send = client.post(
            f"/conversations/{digital_id}/send",
            data={"csrf_token": csrf, "message": "Nao deve enviar"},
            headers={"accept": "application/json"},
        )
        assert blocked_send.status_code == 404

        # A restricao nao atinge a Geral Comercial.
        general = client.get(f"/conversations/{general_id}")
        assert general.status_code == 200
        assert f"GERAL_PERMITIDA_{suffix}" in general.text

        # Vinculo ativo libera, sem precisar trocar responsavel ou fila.
        with SessionLocal() as db:
            membership = ServiceQueueMember(
                queue_id=digital_queue_id, user_id=seller_id, active=True,
            )
            db.add(membership)
            db.commit()
            membership_id = membership.id
        allowed = client.get(f"/conversations/{digital_id}")
        assert allowed.status_code == 200
        assert f"SEGREDO_DIGITAL_{suffix}" in allowed.text
        allowed_api = client.get(f"/api/conversations/{digital_id}/messages?after_id=0")
        assert allowed_api.status_code == 200

        # Revogou a participacao? O bloqueio deve ser imediato.
        with SessionLocal() as db:
            db.get(ServiceQueueMember, membership_id).active = False
            db.commit()
        assert client.get(f"/conversations/{digital_id}", follow_redirects=False).status_code == 303
        assert client.get(f"/api/conversations/{digital_id}/messages?after_id=0").status_code == 404
        assert client.get(f"/conversations/{general_id}").status_code == 200

        # O papel SUPERVISOR conserva exatamente a regra ja existente.
        with SessionLocal() as db:
            supervisor = db.get(User, supervisor_id)
            visible = set(db.scalars(apply_conversation_scope(
                select(Conversation.id).where(
                    Conversation.id.in_([digital_id, general_id])
                ),
                supervisor,
                inbox=False,
            )))
            assert visible == {digital_id, general_id}
