"""UI administrativa e bloqueio manual no chat."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, GlobalContactBlacklist


def _csrf(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found
    return found.group(1)


def _admin_login(client: TestClient) -> None:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local",
        "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303


def test_global_blacklist_routes_and_chat_button():
    with TestClient(app) as client:
        _admin_login(client)
        board = client.get("/blacklist")
        assert board.status_code == 200
        assert "Blacklist de mensagens" in board.text
        assert 'action="/blacklist/import"' in board.text
        sample = client.get("/blacklist/modelo.csv")
        assert sample.status_code == 200
        assert "telefone;nome;motivo" in sample.text

        # Usa um contato exclusivo, sem interferir nas campanhas reais da base.
        with SessionLocal() as db:
            phone = "5555999876541"
            contact = db.scalar(select(Contact).where(Contact.phone == phone))
            if contact is None:
                contact = Contact(name="Cliente optout teste", phone=phone)
                db.add(contact)
                db.flush()
            conversation = Conversation(contact_id=contact.id, status="EM_ATENDIMENTO")
            db.add(conversation)
            db.commit()
            conv_id = conversation.id

        detail = client.get(f"/conversations/{conv_id}")
        assert detail.status_code == 200
        assert f'action="/conversations/{conv_id}/blacklist"' in detail.text
        response = client.post(f"/conversations/{conv_id}/blacklist", data={
            "csrf_token": _csrf(detail.text),
            "reason": "Não enviar mensagens a este cliente",
        }, follow_redirects=False)
        assert response.status_code == 303
        blocked_detail = client.get(f"/conversations/{conv_id}")
        assert "Blacklist global" in blocked_detail.text
        assert "Contato na blacklist global" in blocked_detail.text
        assert 'class="chat-compose"' not in blocked_detail.text
        with SessionLocal() as db:
            number = db.scalar(select(GlobalContactBlacklist).where(
                GlobalContactBlacklist.phone == phone
            ))
            assert number is not None and number.active is True
            assert number.source == "CONVERSA"

        bad = client.post("/blacklist/add", data={
            "csrf_token": _csrf(board.text),
            "phone": "1234",
        }, follow_redirects=False)
        assert bad.status_code == 303

        csv = "telefone;nome;motivo\n5555999876542;Outro cliente;Não deseja receber\n"
        upload = client.post("/blacklist/import", data={
            "csrf_token": _csrf(client.get("/blacklist").text),
        }, files={"file": ("contatos.csv", csv.encode("utf-8"), "text/csv")}, follow_redirects=False)
        assert upload.status_code == 303
        assert "5555999876542" in client.get("/blacklist").text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
