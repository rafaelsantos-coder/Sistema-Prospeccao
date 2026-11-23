"""Privacidade: cada atendimento e isolado; histórico integral só na gestão."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, ConversationAssignment, Message
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


def test_conversation_does_not_merge_legacy_phone_history_or_transfers():
    with TestClient(app) as client:
        _login_admin(client)
        db = SessionLocal()
        try:
            now = utcnow()
            old = Contact(phone="55974826139", name="Cliente Antigo")
            current = Contact(phone="5555974826139", name="Cliente Atual")
            unrelated = Contact(phone="5555974826140", name="Outro Cliente")
            db.add_all([old, current, unrelated])
            db.flush()

            previous = Conversation(
                contact_id=old.id, source="WHATSAPP", status="ENCERRADO",
                created_at=now - timedelta(days=400),
            )
            active = Conversation(
                contact_id=current.id, source="WHATSAPP", status="EM_ATENDIMENTO",
                created_at=now,
            )
            other = Conversation(
                contact_id=unrelated.id, source="WHATSAPP", status="ENCERRADO",
            )
            db.add_all([previous, active, other])
            db.flush()
            db.add_all([
                Message(
                    conversation_id=previous.id, contact_id=old.id,
                    direction="OUTBOUND", message_type="TEXT",
                    body="Promessa comercial de 400 dias atras",
                    created_at=now - timedelta(days=400),
                ),
                Message(
                    conversation_id=active.id, contact_id=current.id,
                    direction="INBOUND", message_type="TEXT",
                    body="Quero retomar a proposta",
                    created_at=now,
                ),
                Message(
                    conversation_id=other.id, contact_id=unrelated.id,
                    direction="INBOUND", message_type="TEXT",
                    body="Segredo de outro numero",
                ),
                ConversationAssignment(
                    conversation_id=previous.id, event="TRANSFERRED",
                    created_at=now - timedelta(days=399),
                ),
            ])
            db.commit()
            active_id = active.id
            previous_id = previous.id
        finally:
            db.close()

        page = client.get(f"/conversations/{active_id}")
        assert page.status_code == 200
        assert "Promessa comercial de 400 dias atras" not in page.text
        assert "Quero retomar a proposta" in page.text
        assert "Segredo de outro numero" not in page.text
        assert "Transferência de atendimento" not in page.text

        api = client.get(f"/api/conversations/{active_id}/messages?after_id=0")
        assert api.status_code == 200
        bodies = [item["body"] for item in api.json()["items"]]
        assert "Promessa comercial de 400 dias atras" not in bodies
        assert "Quero retomar a proposta" in bodies
        assert "Segredo de outro numero" not in bodies

        # O gestor continua podendo auditar a conversa antiga no relatorio.
        report = client.get(f"/reports/whatsapp/conversations/{previous_id}.csv")
        assert report.status_code == 200
        assert "Promessa comercial de 400 dias atras" in report.content.decode("utf-8-sig")


def test_history_phone_does_not_guess_ninth_digit():
    from app.services.scoring import normalize_phone

    assert normalize_phone("55 55 97482-6139") == "5555974826139"
    assert normalize_phone("55 55 7482-6139") == "555574826139"
    assert normalize_phone("55 55 97482-6139") != normalize_phone(
        "55 55 7482-6139"
    )
