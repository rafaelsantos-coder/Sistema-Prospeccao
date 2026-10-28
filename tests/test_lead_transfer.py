from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Lead, User
from app.security import hash_password
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


def test_crm_list_has_transfer_button_for_manager():
    with TestClient(app) as client:
        _login(client, "admin@sulnet.local", "Sulnet@2026!")
        page = client.get("/leads")
        assert page.status_code == 200
        assert "Transferir" in page.text
        assert 'id="lead-transfer-modal"' in page.text


def test_transfer_lead_to_seller_updates_seller_dashboard_and_crm():
    seller_email = "vendedor-transferencia@sulnet.local"
    seller_password = "SenhaTransfer@2026"
    business_name = "Lead Transferência CRM"

    with TestClient(app) as client:
        db = SessionLocal()
        try:
            seller = db.scalar(select(User).where(User.email == seller_email))
            if not seller:
                seller = User(
                    name="Vendedor Transferência",
                    email=seller_email,
                    password_hash=hash_password(seller_password),
                    role="CONSULTOR",
                    team="B2B",
                    active=True,
                    must_change_password=False,
                )
                db.add(seller)
                db.flush()

            lead = db.scalar(select(Lead).where(Lead.business_name == business_name))
            if not lead:
                lead = Lead(
                    business_name=business_name,
                    contact_name="Cliente Teste",
                    phone="5555988812345",
                    segment="Empresa",
                    city="Santa Rosa",
                    source="MANUAL",
                    origin_channel="WHATSAPP",
                    team="B2B",
                    stage="NOVO",
                    score=50,
                    temperature="MORNO",
                    priority="ALTA",
                    next_action_at=utcnow(),
                    assigned_user_id=None,
                    created_by_id=None,
                )
                db.add(lead)
                db.flush()
            else:
                lead.assigned_user_id = None
                lead.priority = "ALTA"
                lead.stage = "NOVO"
                lead.next_action_at = utcnow()
            db.commit()
            seller_id = seller.id
            lead_id = lead.id
        finally:
            db.close()

        token = _login(client, "admin@sulnet.local", "Sulnet@2026!")
        response = client.post(
            f"/leads/{lead_id}/assign",
            data={
                "csrf_token": token,
                "assigned_user_id": str(seller_id),
                "return_to": "/leads",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/leads"

        client.post(
            "/logout",
            data={"csrf_token": _csrf(client.get("/dashboard").text)},
            follow_redirects=False,
        )
        _login(client, seller_email, seller_password)

        crm = client.get("/leads")
        assert crm.status_code == 200
        assert business_name in crm.text

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert business_name in dashboard.text

        db = SessionLocal()
        try:
            transferred = db.get(Lead, lead_id)
            assert transferred is not None
            assert transferred.assigned_user_id == seller_id
        finally:
            db.close()
