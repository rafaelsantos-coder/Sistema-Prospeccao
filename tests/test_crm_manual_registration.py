"""Testes da integração manual: nunca chamam um CRM real."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Conversation, User
from app.services import crm_manual_registration as manual


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
    token = _csrf(client.get("/login").text)
    login = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert login.status_code == 303


@pytest.mark.parametrize(
    ("name", "username", "funnel", "stage"),
    [
        ("Maria Gabriela Paim Peno", "maria.peno", 16, 55),
        ("Claudia Linde", "claudia.linde", 16, 55),
        ("Natine Dauana Dalla Lana Hirt", "natine.hirt", 17, 63),
        ("Fabiano Braga", "fabiano.braga", 17, 63),
        ("Gabriele Naiane Barbosa", "gabriele.barbosa", 17, 63),
        ("Michele Zandona", "michele.zandona", 17, 63),
    ],
)
def test_seller_mapping_is_exact(name, username, funnel, stage):
    actor = User(name=name, email=f"{username}@example.org", password_hash="x", active=True)
    target = manual.seller_for_user(actor)
    assert target is not None
    assert (target.username, target.funnel_id, target.stage_id) == (username, funnel, stage)
    actor.active = False
    assert manual.seller_for_user(actor) is None


@pytest.mark.parametrize(
    ("name", "username", "crm_user_id"),
    [
        ("Gabriele Naiane Barbosa", "gabriele.barbosa", 42),
        ("Michele Zandona", "michele.zandona", 43),
    ],
)
def test_new_santa_rosa_sellers_allow_upgrade_without_inventing_upgrade_stage(
    name, username, crm_user_id,
):
    user = User(name=name, email="conta@sulnet.local", password_hash="x", active=True)
    seller = manual.seller_for_user(user)
    assert seller is not None
    assert seller.username == username
    assert seller.user_id == crm_user_id
    assert seller.funnel_id == 17
    assert seller.stage_id == 63
    assert seller.additional_funnel_ids == (23,)
    assert manual.seller_accepts_funnel(seller, 17)
    assert manual.seller_accepts_funnel(seller, 23)
    assert not manual.seller_accepts_funnel(seller, 16)
    assert manual._verified_stage(seller, 17, 63)
    assert not manual._verified_stage(seller, 17, 55)  # Criação deve confirmar etapa inicial
    assert manual._verified_stage(seller, 17, 65, newly_created=False)  # Já estava em negociação
    assert not manual._verified_stage(seller, 16, 65, newly_created=False)
    assert manual._verified_stage(seller, 23, 77)  # Etapa informada pelo CRM, sem presumir ID
    assert not manual._verified_stage(seller, 23, None)
    assert not manual._verified_stage(seller, 23, 0)
    user.active = False
    assert manual.seller_for_user(user) is None


def test_crm_search_rejects_incomplete_pagination_and_unknown_shape():
    with pytest.raises(manual.CRMManualError):
        manual._records({"items": [{"id": 1}], "total": 2})
    with pytest.raises(manual.CRMManualError):
        manual._records({"unexpected": True})
    assert manual._phone_key("55 (55) 99900-2508") == manual._phone_key("55999002508")


def test_manual_registration_uses_correct_seller_and_does_not_duplicate(monkeypatch):
    calls = []

    class FakeCRM:
        bearer_token = "read-only-credential"

        def find_opportunities_by_phone(self, phone):
            calls.append(("lookup", phone))
            return {"items": []}

        def create_seller_lead(self, **kwargs):
            calls.append(("create", kwargs))
            return {
                "id": 17234,
                "protocol": "CRM-2026-17234",
                "assignedUserId": 36,
                "funnelId": 17,
                "stageId": 63,
            }

    monkeypatch.setattr(
        manual.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM())
    )

    with TestClient(app) as client:
        _login(client)
        lead_page = client.get("/leads/1")
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": _csrf(lead_page.text)},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        path = opened.headers["location"]
        conversation_id = int(path.rstrip("/").split("/")[-1])

        with SessionLocal() as db:
            seller = db.scalar(select(User).where(User.email == "fabiano.crm.manual.test@sulnet.local"))
            if seller is None:
                seller = User(
                    name="Fabiano Braga",
                    email="fabiano.crm.manual.test@sulnet.local",
                    password_hash="test-hash",
                    role="CONSULTOR",
                    active=True,
                )
                db.add(seller)
                db.flush()
            conversation = db.get(Conversation, conversation_id)
            conversation.assigned_user_id = seller.id
            contact_id = conversation.contact_id
            db.commit()

        detail = client.get(path)
        assert detail.status_code == 200
        assert 'data-crm-manual-open' in detail.text
        assert "Sulnet Prospecta" in detail.text

        first = client.post(
            path + "/register-crm",
            data={"csrf_token": _csrf(detail.text), "client_name": "Joao da Silva"},
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert len([c for c in calls if c[0] == "create"]) == 1
        assert calls[-1][1]["seller_username"] == "fabiano.braga"
        assert calls[-1][1]["origin"] == "Sulnet Prospecta"

        detail_after = client.get(path)
        assert "Registrado no CRM" in detail_after.text
        second = client.post(
            path + "/register-crm",
            data={"csrf_token": _csrf(detail_after.text), "client_name": "Joao da Silva"},
            follow_redirects=False,
        )
        assert second.status_code == 303
        assert len([c for c in calls if c[0] == "create"]) == 1

        with SessionLocal() as db:
            registered = list(db.scalars(select(AuditLog).where(
                AuditLog.entity_type == "contact",
                AuditLog.entity_id == str(contact_id),
                AuditLog.action == manual.ACTION_REGISTERED,
            )))
            assert registered


def test_seller_wrong_owner_is_blocked_without_creating(monkeypatch):
    class ExistingCRM:
        bearer_token = "read-only-credential"

        def find_opportunities_by_phone(self, phone):
            return {
                "items": [{
                    "id": 1001,
                    "clientPhone": phone,
                    "assignedUserId": 25,
                    "funnelId": 17,
                    "status": "NOVO",
                }]
            }

        def create_seller_lead(self, **kwargs):
            raise AssertionError("Nunca criar lead com proprietário diferente")

    monkeypatch.setattr(
        manual.SulnetCRMClient, "from_settings", classmethod(lambda cls: ExistingCRM())
    )
    with TestClient(app) as client:
        _login(client)
        page = client.get("/leads/1")
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": _csrf(page.text)}, follow_redirects=False
        )
        path = opened.headers["location"]
        cid = int(path.rstrip("/").split("/")[-1])
        with SessionLocal() as db:
            seller = db.scalar(select(User).where(User.email == "fabiano.crm.manual.test@sulnet.local"))
            if seller is None:
                seller = User(
                    name="Fabiano Braga",
                    email="fabiano.crm.manual.test@sulnet.local",
                    password_hash="test-hash",
                    role="CONSULTOR",
                    active=True,
                )
                db.add(seller)
                db.flush()
            conv = db.get(Conversation, cid)
            conv.assigned_user_id = seller.id
            db.execute(delete(AuditLog).where(
                AuditLog.entity_type == "contact",
                AuditLog.entity_id == str(conv.contact_id),
                AuditLog.action.in_((manual.ACTION_REGISTERED, manual.ACTION_UNCERTAIN, manual.ACTION_REVIEW)),
            ))
            db.commit()

        page = client.get(path)
        response = client.post(path + "/register-crm", data={
            "csrf_token": _csrf(page.text), "client_name": "Joao da Silva",
        }, follow_redirects=True)
        assert response.status_code == 200
        assert "outro responsável" in response.text
