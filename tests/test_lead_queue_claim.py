from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Activity,
    Lead,
    LeadQueuePreference,
    ServiceQueue,
    ServiceQueueMember,
    User,
)
from app.security import hash_password
from app.services.seed import _release_unclaimed_queue_prospecting_leads
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/dashboard").text)


def _seller(db, *, email: str, name: str) -> User:
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        return existing
    user = User(
        name=name,
        email=email,
        password_hash=hash_password("SenhaFila@2026"),
        role="CONSULTOR",
        team="LOJA",
        active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def test_queue_consultant_sees_unassigned_lead_and_can_claim_it():
    business_name = "Lead Disponível Fila Teste"
    seller_email = "seller.queue.claim@sulnet.local"
    second_email = "seller.queue.second@sulnet.local"

    with TestClient(app) as client:
        db = SessionLocal()
        try:
            seller = _seller(db, email=seller_email, name="Seller Queue Claim")
            second = _seller(db, email=second_email, name="Seller Queue Second")
            queue = ServiceQueue(
                name="Fila Loja Claim",
                code="QUEUE-CLAIM-TEST",
                team="LOJA",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add_all(
                [
                    ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True),
                    ServiceQueueMember(queue_id=queue.id, user_id=second.id, active=True),
                ]
            )
            lead = Lead(
                business_name=business_name,
                contact_name="Cliente",
                phone="5555999997771",
                segment="Importação CSV",
                city="Santa Rosa",
                source="IMPORTACAO",
                origin_channel="WHATSAPP",
                team="LOJA",
                stage="NOVO",
                score=0,
                temperature="FRIO",
                priority="NORMAL",
                assigned_user_id=None,
                created_by_id=None,
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="CSV_DISPATCH",
                )
            )
            db.commit()
            lead_id = lead.id
            seller_id = seller.id
        finally:
            db.close()

        token = _login(client, seller_email, "SenhaFila@2026")
        page = client.get("/leads")
        assert page.status_code == 200
        assert business_name in page.text
        assert "Disponível para assumir" in page.text
        assert "Assumir" in page.text

        response = client.post(
            f"/leads/{lead_id}/claim",
            data={
                "csrf_token": token,
                "return_to": "/leads",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/leads"

        db = SessionLocal()
        try:
            claimed = db.get(Lead, lead_id)
            assert claimed is not None
            assert claimed.assigned_user_id == seller_id
        finally:
            db.close()

        client.post(
            "/logout",
            data={"csrf_token": _csrf(client.get("/dashboard").text)},
            follow_redirects=False,
        )
        _login(client, second_email, "SenhaFila@2026")
        second_page = client.get("/leads")
        assert second_page.status_code == 200
        assert business_name not in second_page.text


def test_release_old_untouched_prospecting_lead_to_queue():
    with TestClient(app):
        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
            queue = ServiceQueue(
                name="Fila Prospecção Liberação",
                code="QUEUE-PROSPECT-RELEASE",
                team="B2B",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            lead = Lead(
                business_name="Prospecção Antiga Sem Atividade",
                phone="5555999997772",
                segment="Oficina",
                city="Santa Rosa",
                source="APIFY",
                origin_channel="WHATSAPP",
                team="B2B",
                stage="NOVO",
                score=40,
                temperature="FRIO",
                priority="NORMAL",
                assigned_user_id=admin.id,
                created_by_id=admin.id,
            )
            db.add(lead)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=lead.id,
                    queue_id=queue.id,
                    source="PROSPECTING",
                )
            )

            worked = Lead(
                business_name="Prospecção Antiga Já Trabalhada",
                phone="5555999997773",
                segment="Oficina",
                city="Santa Rosa",
                source="APIFY",
                origin_channel="WHATSAPP",
                team="B2B",
                stage="NOVO",
                score=40,
                temperature="FRIO",
                priority="NORMAL",
                assigned_user_id=admin.id,
                created_by_id=admin.id,
            )
            db.add(worked)
            db.flush()
            db.add(
                LeadQueuePreference(
                    lead_id=worked.id,
                    queue_id=queue.id,
                    source="PROSPECTING",
                )
            )
            db.add(
                Activity(
                    lead_id=worked.id,
                    user_id=admin.id,
                    kind="WHATSAPP",
                    status="CONCLUIDA",
                    channel="WHATSAPP",
                    description="Já trabalhada",
                    completed_at=utcnow(),
                )
            )
            db.commit()
            lead_id = lead.id
            worked_id = worked.id

            released = _release_unclaimed_queue_prospecting_leads(db)
            db.commit()
            assert released >= 1
            assert db.get(Lead, lead_id).assigned_user_id is None
            assert db.get(Lead, worked_id).assigned_user_id == admin.id
        finally:
            db.close()
