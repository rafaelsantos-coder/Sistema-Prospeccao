"""Conciliação gerencial de envios: acesso por permissão, sem novos disparos."""
from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign, CampaignRecipient, CampaignDispatch,
    CampaignDispatchRecipient, Lead, ServiceQueue,
)
from app.services.access_control import has_permission
from app.timeutils import utcnow


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token is not None
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_delivery_review_permission_respects_role_and_explicit_grants():
    manager = SimpleNamespace(role="GESTOR", active=True, access_permissions=None)
    consultant = SimpleNamespace(
        role="CONSULTOR", active=True,
        access_permissions='["dispatches.view", "dispatches.reconcile"]',
    )
    restricted_manager = SimpleNamespace(
        role="GESTOR", active=True, access_permissions='["dispatches.view"]'
    )
    assert has_permission(manager, "dispatches.reconcile")
    assert not has_permission(consultant, "dispatches.reconcile")
    assert not has_permission(restricted_manager, "dispatches.reconcile")


def test_delivery_review_requires_authentication():
    with TestClient(app) as client:
        response = client.get("/dispatches/reconciliation", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_delivery_review_shows_both_sources_without_mutating_status():
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        _login_admin(client)
        with SessionLocal() as db:
            campaign = Campaign(
                name="REVIEW-CAMP-" + suffix, channel="WHATSAPP",
                status="APROVADA", daily_limit=20, team="DIGITAL",
                sequence_json="[]",
            )
            queue = ServiceQueue(
                name="REVIEW-QUEUE-" + suffix,
                code="REVIEW-" + suffix, team="DIGITAL",
                routing_strategy="ROUND_ROBIN", active=True,
            )
            db.add_all([campaign, queue])
            db.flush()
            lead = Lead(
                business_name="REVIEW-BUSINESS-" + suffix,
                contact_name="REVIEW-CONTACT-" + suffix,
                phone="5555999998811", source="MANUAL", team="DIGITAL",
                segment="Teste interno",
            )
            db.add(lead)
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=30, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            unknown = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, name="REVIEW-UNKNOWN-" + suffix,
                phone="5555999998812", message_body="Apenas simulação",
                status="INDETERMINADO", attempts=1, error="Timeout simulado",
            )
            sending = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, name="REVIEW-SENDING-" + suffix,
                phone="5555999998813", message_body="Apenas simulação",
                status="ENVIANDO", attempts=1,
                updated_at=utcnow() - timedelta(minutes=25),
            )
            legacy = CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead.id,
                message_body="Apenas simulação", status="INDETERMINADO",
                attempts=1, error="Confirmação não recebida",
            )
            db.add_all([unknown, sending, legacy])
            db.commit()
            keys = (unknown.id, sending.id, legacy.id)

        page = client.get("/dispatches/reconciliation")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "Envios a conferir" in page.text
        assert "REVIEW-UNKNOWN-" + suffix in page.text
        assert "REVIEW-SENDING-" + suffix in page.text
        assert "REVIEW-CONTACT-" + suffix in page.text
        assert "Processamento acima de 10 min" in page.text
        assert "não envia mensagens" in page.text

        csv = client.get("/dispatches/reconciliation?source=DISPATCH")
        assert csv.status_code == 200
        assert "REVIEW-UNKNOWN-" + suffix in csv.text
        assert "REVIEW-SENDING-" + suffix in csv.text
        assert "REVIEW-CONTACT-" + suffix not in csv.text

        campaigns = client.get(
            "/dispatches/reconciliation?source=CAMPAIGN&status=INDETERMINADO"
        )
        assert campaigns.status_code == 200
        assert "REVIEW-CONTACT-" + suffix in campaigns.text
        assert "REVIEW-UNKNOWN-" + suffix not in campaigns.text
        assert "REVIEW-SENDING-" + suffix not in campaigns.text

        with SessionLocal() as db:
            assert db.get(CampaignDispatchRecipient, keys[0]).status == "INDETERMINADO"
            assert db.get(CampaignDispatchRecipient, keys[1]).status == "ENVIANDO"
            assert db.get(CampaignRecipient, keys[2]).status == "INDETERMINADO"
            assert db.get(CampaignDispatchRecipient, keys[0]).attempts == 1
