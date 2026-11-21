"""SulIA registra uma oportunidade única no CRM no início do atendimento."""
from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile, AIClosingState, AuditLog, Contact, Conversation, Message,
)
from app.services import crm_prospect_sync as sync


def _login(client):
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303


def _conversation(db, number, *, with_ai_reply):
    agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital"))
    assert agent is not None
    contact = Contact(phone=number, name="Cliente IA de Teste")
    conversation = Conversation(
        contact=contact, assigned_user_id=agent.user_id, status="EM_ATENDIMENTO",
    )
    db.add(conversation)
    db.flush()
    if with_ai_reply:
        db.add(Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            sender_user_id=agent.user_id,
            direction="OUTBOUND", message_type="TEXT",
            body="Olá! Posso ajudar com a contratação de internet?", status="SENT",
        ))
    db.commit()
    return conversation.id, agent.id


def _fake_crm(monkeypatch, created):
    class FakeCRM:
        bearer_token = "for-tests"

        def create_opportunity(self, payload):
            created.append(payload)
            return {"opportunityId": 18402, "created": True}

        def add_opportunity_tag(self, *args):
            return None

        def add_opportunity_note(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        sync.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM()),
    )


def test_automatic_registers_at_first_ai_outbound_without_cpf_or_inbound(monkeypatch):
    created = []
    _fake_crm(monkeypatch, created)
    monkeypatch.setattr(
        "app.services.ai_sdr.binding_for_conversation",
        lambda db, conversation_id: SimpleNamespace(active=True),
    )
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            cid, _ = _conversation(db, "5555980008157", with_ai_reply=True)
            assert sync.sync_prospects(db, conversation_id=cid, limit=1) == 1
            assert sync.sync_prospects(db, conversation_id=cid, limit=1) == 0
            assert sync.ai_crm_opportunity_id(db, cid) == 18402
            assert created[0]["runConsult"] is False
            assert created[0]["externalId"] == f"prospecta-conversation-{cid}"
            assert "cpf" not in created[0]
            assert len(created) == 1
        html = client.get(f"/conversations/{cid}").text
        assert "Registrado no CRM #18402" in html
        assert "Contato espontâneo</strong>" not in html


def test_manager_can_click_register_crm_for_ia_before_first_reply(monkeypatch):
    created = []
    _fake_crm(monkeypatch, created)
    monkeypatch.setattr(
        "app.services.ai_sdr.binding_for_conversation",
        lambda db, conversation_id: SimpleNamespace(active=True),
    )
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            cid, _ = _conversation(db, "5555980008158", with_ai_reply=False)
        page = client.get(f"/conversations/{cid}")
        assert page.status_code == 200
        assert "data-crm-manual-open" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/conversations/{cid}/register-crm",
            data={"csrf_token": token, "client_name": "Cliente IA de Teste"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert len(created) == 1
        registered = client.get(f"/conversations/{cid}").text
        assert "Registrado no CRM #18402" in registered


def test_existing_closer_crm_id_is_reused_not_created(monkeypatch):
    created = []
    _fake_crm(monkeypatch, created)
    monkeypatch.setattr(
        "app.services.ai_sdr.binding_for_conversation",
        lambda db, conversation_id: SimpleNamespace(active=True),
    )
    with TestClient(app):
        with SessionLocal() as db:
            cid, aid = _conversation(db, "5555980008159", with_ai_reply=True)
            db.add(AIClosingState(
                conversation_id=cid,
                agent_id=aid,
                external_id=f"prospecta-conversation-{cid}",
                opportunity_id=19876, step="CPF",
            ))
            db.commit()
            assert sync.sync_prospects(db, conversation_id=cid, limit=1) == 1
            assert sync.ai_crm_opportunity_id(db, cid) == 19876
            assert sync.sync_prospects(db, conversation_id=cid, limit=1) == 0
            assert not created


def test_campaign_send_without_ai_response_does_not_register(monkeypatch):
    created = []
    _fake_crm(monkeypatch, created)
    monkeypatch.setattr(
        "app.services.ai_sdr.binding_for_conversation",
        lambda db, conversation_id: SimpleNamespace(active=True),
    )
    with TestClient(app):
        with SessionLocal() as db:
            cid, _ = _conversation(db, "5555980008160", with_ai_reply=False)
            assert sync.sync_prospects(db, conversation_id=cid, limit=1) == 0
            assert not created
