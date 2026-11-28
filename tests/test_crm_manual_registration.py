"""Testes da integração manual: nunca chamam um CRM real."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Contact, Conversation, User
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
        ("Jenifer Garcia Dutra", "jenifer.dutra", 18, 71),
        ("Jennifer Garcia Dutra", "jenifer.dutra", 18, 71),
        ("Daiane Mattos", "daiane.mattos", 18, 71),
        ("Daiane dos Santos Mattos", "daiane.mattos", 18, 71),
        ("Andressa Reus", "andressa.reus", 18, 71),
        ("Larissa Almeida", "larissa.almeida", 15, 47),
        ("Larissa da Rosa de Almeida", "larissa.almeida", 15, 47),
        ("Rafael Trennepohl", "rafael.santos", 15, 47),
        ("rafael.vendedor", "rafael.santos", 15, 47),
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


@pytest.mark.parametrize(
    ("name", "username", "user_id", "funnel_id", "funnel_name", "stage_id"),
    [
        ("Jenifer Garcia Dutra", "jenifer.dutra", 8, 18, "Digital", 71),
        ("Daiane Mattos", "daiane.mattos", 11, 18, "Digital", 71),
        ("Andressa Reus", "andressa.reus", 4, 18, "Digital", 71),
        ("Larissa Almeida", "larissa.almeida", 40, 15, "Missões", 47),
    ],
)
def test_crm_digital_and_missoes_sellers_have_verified_funnels(
    name, username, user_id, funnel_id, funnel_name, stage_id,
):
    seller = manual.seller_for_user(User(
        name=name, email="conta@sulnet.local", password_hash="x", active=True,
    ))
    assert seller is not None
    assert (
        seller.username, seller.user_id, seller.funnel_id,
        seller.funnel_name, seller.stage_id,
    ) == (username, user_id, funnel_id, funnel_name, stage_id)
    assert seller.additional_funnel_ids == (23, 24)
    assert manual._verified_stage(seller, funnel_id, stage_id)
    assert not manual._verified_stage(seller, funnel_id, 55)
    assert not manual._verified_stage(seller, funnel_id, 63)
    # Upgrade e troca de endereço têm etapas próprias; não presumir estágio.
    assert manual.seller_accepts_funnel(seller, 23)
    assert manual.seller_accepts_funnel(seller, 24)
    assert manual._verified_stage(seller, 23, 100)
    assert manual._verified_stage(seller, 24, 100)
    assert not manual._verified_stage(seller, 23, None)
    assert not manual._verified_stage(seller, 24, 0)
    assert not manual.seller_accepts_funnel(seller, 16)
    assert not manual.seller_accepts_funnel(seller, 17)
    assert not manual._verified_stage(seller, 16, 55, newly_created=False)
    assert not manual._verified_stage(seller, 17, 63, newly_created=False)


def test_crm_seller_aliases_remain_same_owner():
    aliases = (
        ("Daiane Mattos", "Daiane dos Santos Mattos"),
        ("Larissa Almeida", "Larissa da Rosa de Almeida"),
        ("Jenifer Garcia Dutra", "Jennifer Garcia Dutra"),
    )
    for official, local_name in aliases:
        seller = manual.seller_for_user(User(
            name=official, email="crm@sulnet.local", password_hash="x", active=True,
        ))
        alias = manual.seller_for_user(User(
            name=local_name, email="prospecta@sulnet.local", password_hash="x", active=True,
        ))
        assert seller is not None
        assert seller == alias
        assert manual.seller_for_user(User(
            name=local_name, email="prospecta@sulnet.local", password_hash="x",
            active=False,
        )) is None


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("Rafael Trennepohl", "rafael.vendedor@sulnet.local"),
        ("rafael.vendedor", "rafael.vendedor@sulnet.local"),
        ("Rafael Trennepohl", "rafael@sulnet.local"),
        ("Nome comercial diferente", "rafael.vendedor@sulnet.local"),
    ],
)
def test_rafael_prospecta_account_maps_to_confirmed_crm_manager(name, email):
    seller = manual.seller_for_user(User(
        name=name, email=email, password_hash="x", role="GESTOR", active=True,
    ))
    assert seller == manual.CRMSeller(
        "rafael.santos", 7, 15, "Missões", 47,
        (16, 17, 18, 11, 3, 13, 20, 22, 23, 24),
    )
    assert manual._verified_stage(seller, 15, 47)
    assert not manual._verified_stage(seller, 15, 71)
    assert all(manual.seller_accepts_funnel(seller, fid) for fid in (
        15, 16, 17, 18, 11, 3, 13, 20, 22, 23, 24
    ))
    assert not manual.seller_accepts_funnel(seller, 999)
    assert manual._verified_stage(seller, 18, 71)  # etapa própria de funil adicional
    assert not manual._verified_stage(seller, 18, None)
    assert manual.seller_for_user(User(
        name=name, email=email, password_hash="x", active=False,
    )) is None


def test_rafael_login_never_impersonates_other_registered_seller():
    assert manual.seller_for_user(User(
        name="Fabiano Braga", email="rafael.vendedor@sulnet.local",
        password_hash="x", active=True,
    )) is None
    assert manual.seller_for_user(User(
        name="Rafael qualquer", email="outro.usuario@sulnet.local",
        password_hash="x", active=True,
    )) is None


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

        def list_active_origins(self):
            return [{"id": 51, "name": "Sulnet Prospecta"}, {"id": 52, "name": "Origem de teste"}]

        def find_opportunities_by_phone(self, phone):
            calls.append(("lookup", phone))
            return {"items": [{"id": 1001, "clientPhone": phone, "assignedUserId": 25}]}

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
        assert 'id="crm-manual-origin"' in detail.text
        catalog = client.get(path + "/crm-origins")
        assert catalog.status_code == 200
        assert catalog.json()["defaultId"] == 51
        assert catalog.json()["origins"][1]["name"] == "Origem de teste"
        invalid = client.post(
            path + "/register-crm",
            data={"csrf_token": _csrf(detail.text), "client_name": "Joao da Silva", "origin_id": "999"},
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        assert not [c for c in calls if c[0] == "create"]

        first = client.post(
            path + "/register-crm",
            data={"csrf_token": _csrf(detail.text), "client_name": "Joao da Silva", "origin_id": "52"},
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert len([c for c in calls if c[0] == "create"]) == 1
        assert calls[-1][1]["seller_username"] == "fabiano.braga"
        assert calls[-1][1]["origin"] == "Origem de teste"

        detail_after = client.get(path)
        assert "Registrado no CRM" in detail_after.text
        second = client.post(
            path + "/register-crm",
            data={"csrf_token": _csrf(detail_after.text), "client_name": "Joao da Silva", "origin_id": "51"},
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


def test_reassigned_conversation_keeps_each_sellers_registration(monkeypatch):
    created = []

    class ExistingCRM:
        bearer_token = "read-only-credential"

        def list_active_origins(self):
            return [{"id": 51, "name": "Sulnet Prospecta"}, {"id": 52, "name": "Origem de teste"}]

        def find_opportunities_by_phone(self, phone):
            return {
                "items": [
                    {"id": 1000, "clientPhone": phone, "assignedUserId": 35},
                    {
                        "id": 1001,
                        "clientPhone": phone,
                        "assignedUserId": 25,
                        "funnelId": 17,
                        "stageId": 65,
                        "status": "NOVO",
                        "protocol": "CRM-NATINE-1001",
                    },
                ]
            }

        def create_seller_lead(self, **kwargs):
            created.append(kwargs)
            return {
                "id": 1002,
                "assignedUserId": 36,
                "funnelId": 17,
                "stageId": 63,
                "protocol": "CRM-FABIANO-1002",
            }

    monkeypatch.setattr(
        manual.SulnetCRMClient, "from_settings", classmethod(lambda cls: ExistingCRM())
    )
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            first_seller = User(
                name="Natine Dauana Dalla Lana Hirt",
                email="natine.crm.reassigned@example.org",
                password_hash="test-hash", role="CONSULTOR", active=True,
            )
            second_seller = User(
                name="Fabiano Braga", email="fabiano.crm.reassigned@example.org",
                password_hash="test-hash", role="CONSULTOR", active=True,
            )
            contact = Contact(phone="5555980001002", name="Joao da Silva")
            conv = Conversation(contact=contact, assigned_user=first_seller)
            db.add_all([conv, second_seller])
            db.commit()
            cid = conv.id
            first_seller_id, second_seller_id = first_seller.id, second_seller.id

        path = f"/conversations/{cid}"
        register_path = path + "/register-crm"
        first = client.post(register_path, data={
            "csrf_token": _csrf(client.get(path).text), "client_name": "Joao da Silva", "origin_id": "51",
        })
        assert first.status_code == 200
        assert "CRM-NATINE-1001" in first.text
        assert not created

        with SessionLocal() as db:
            db.get(Conversation, cid).assigned_user_id = second_seller_id
            db.commit()

        second_page = client.get(path).text
        assert f'action="{register_path}"' in second_page
        assert "CRM-NATINE-1001" not in second_page
        second = client.post(register_path, data={
            "csrf_token": _csrf(second_page), "client_name": "Joao da Silva", "origin_id": "51",
        })
        assert second.status_code == 200
        assert "Oportunidade registrada no CRM." in second.text
        assert "CRM-FABIANO-1002" in second.text
        assert [item["seller_username"] for item in created] == ["fabiano.braga"]

        with SessionLocal() as db:
            db.get(Conversation, cid).assigned_user_id = first_seller_id
            db.commit()

        first_page = client.get(path).text
        assert "CRM-NATINE-1001" in first_page
        assert "CRM-FABIANO-1002" not in first_page
