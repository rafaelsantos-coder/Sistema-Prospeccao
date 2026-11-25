"""Campanhas de origem: match do texto padrão e registro CRM com origem dinâmica."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, OriginCampaign, User
from app.services import crm_manual_registration as manual
from app.services.origin_campaigns import (
    DEFAULT_CRM_ORIGIN,
    fold_match_text,
    match_origin_from_texts,
    resolve_conversation_origin,
    validate_campaign_fields,
)


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


def test_fold_and_validate_campaign_fields():
    assert "black november" in fold_match_text("Olá, vim do anúncio de BLACK NOVEMBER!")
    name, match, origin, link = validate_campaign_fields(
        name="  Black November  ",
        match_text="Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet",
        crm_origin="Facebook Ads · Black November",
        link="https://example.com/ads/black",
    )
    assert name == "Black November"
    assert "BLACK NOVEMBER" in match
    assert origin.startswith("Facebook")
    assert link.startswith("https://")
    with pytest.raises(ValueError):
        validate_campaign_fields(name="X", match_text="curto", crm_origin="Origem")
    with pytest.raises(ValueError):
        validate_campaign_fields(
            name="Campanha",
            match_text="Olá, vim do anúncio de BLACK NOVEMBER",
            crm_origin="Origem",
            link="ftp://invalido",
        )


def test_longest_match_wins_over_shorter_pattern():
    short = OriginCampaign(
        id=1,
        name="Genérica",
        match_text="Olá, vim do anúncio de",
        crm_origin="Tráfego pago genérico",
        active=True,
    )
    long = OriginCampaign(
        id=2,
        name="Black November",
        match_text="Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet",
        crm_origin="Facebook Ads · Black November",
        active=True,
    )
    bodies = [
        "Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet em Santo Ângelo!"
    ]
    match = match_origin_from_texts([short, long], bodies)
    assert match is not None
    assert match.campaign_id == 2
    assert match.crm_origin == "Facebook Ads · Black November"


def test_inactive_campaign_is_ignored():
    campaign = OriginCampaign(
        id=9,
        name="Inativa",
        match_text="Olá, vim do anúncio de BLACK NOVEMBER",
        crm_origin="Não deve aparecer",
        active=False,
    )
    assert match_origin_from_texts(
        [campaign],
        ["Olá, vim do anúncio de BLACK NOVEMBER e quero assinar"],
    ) is None


def test_resolve_conversation_origin_from_inbound_message():
    with TestClient(app):
        with SessionLocal() as db:
            contact = Contact(phone="5555980012345", name="Brunna Cilios")
            seller = User(
                name="Claudia Linde",
                email="claudia.origin.match@sulnet.local",
                password_hash="x",
                role="CONSULTOR",
                active=True,
            )
            conversation = Conversation(contact=contact, assigned_user=seller)
            campaign = OriginCampaign(
                name="Black November Match",
                match_text="Olá, vim do anúncio de BLACK NOVEMBER",
                crm_origin="Facebook Ads · Black November",
                link="https://example.com/black",
                active=True,
            )
            db.add_all([conversation, campaign])
            db.flush()
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body=(
                        "Olá, vim do anúncio de BLACK NOVEMBER e quero "
                        "assinar Sulnet em Santo Ângelo!"
                    ),
                    status="RECEIVED",
                )
            )
            db.commit()
            conversation_id = conversation.id

            origin, match = resolve_conversation_origin(db, conversation_id)
            assert origin == "Facebook Ads · Black November"
            assert match is not None
            assert match.campaign_name == "Black November Match"


def test_settings_origin_campaigns_crud_and_crm_register_uses_match(monkeypatch):
    calls = []

    class FakeCRM:
        bearer_token = "read-only-credential"

        def find_opportunities_by_phone(self, phone):
            return {"items": []}

        def create_seller_lead(self, **kwargs):
            calls.append(kwargs)
            return {
                "id": 99001,
                "protocol": "CRM-ORIGIN-99001",
                "assignedUserId": 37,
                "funnelId": 16,
                "stageId": 55,
            }

    monkeypatch.setattr(
        manual.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM())
    )

    with TestClient(app) as client:
        _login(client)
        page = client.get("/settings/origin-campaigns")
        assert page.status_code == 200
        assert "Campanhas de origem" in page.text

        created = client.post(
            "/settings/origin-campaigns",
            data={
                "csrf_token": _csrf(page.text),
                "name": "Black November",
                "match_text": "Olá, vim do anúncio de BLACK NOVEMBER",
                "crm_origin": "Facebook Ads · Black November",
                "link": "https://example.com/black-november",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        with SessionLocal() as db:
            campaign = db.scalar(
                select(OriginCampaign).where(OriginCampaign.name == "Black November")
            )
            assert campaign is not None
            assert campaign.active is True

            seller = User(
                name="Claudia Linde",
                email="claudia.origin.crm@sulnet.local",
                password_hash="x",
                role="CONSULTOR",
                active=True,
            )
            contact = Contact(phone="5555980099887", name="Brunna_Cilios")
            conversation = Conversation(contact=contact, assigned_user=seller)
            db.add(conversation)
            db.flush()
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body=(
                        "Olá, vim do anúncio de BLACK NOVEMBER e quero "
                        "assinar Sulnet em Santo Ângelo!"
                    ),
                    status="RECEIVED",
                )
            )
            db.commit()
            conversation_id = conversation.id

        detail = client.get(f"/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert "Facebook Ads · Black November" in detail.text
        assert "Black November" in detail.text

        registered = client.post(
            f"/conversations/{conversation_id}/register-crm",
            data={
                "csrf_token": _csrf(detail.text),
                "client_name": "Brunna Cilios",
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303
        assert len(calls) == 1
        assert calls[0]["origin"] == "Facebook Ads · Black November"
        assert calls[0]["seller_username"] == "claudia.linde"


def test_crm_register_keeps_default_origin_without_match(monkeypatch):
    calls = []

    class FakeCRM:
        bearer_token = "read-only-credential"

        def find_opportunities_by_phone(self, phone):
            return {"items": []}

        def create_seller_lead(self, **kwargs):
            calls.append(kwargs)
            return {
                "id": 99002,
                "protocol": "CRM-DEFAULT-99002",
                "assignedUserId": 37,
                "funnelId": 16,
                "stageId": 55,
            }

    monkeypatch.setattr(
        manual.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM())
    )

    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            seller = User(
                name="Claudia Linde",
                email="claudia.origin.default@sulnet.local",
                password_hash="x",
                role="CONSULTOR",
                active=True,
            )
            contact = Contact(phone="5555980077665", name="Cliente Avulso")
            conversation = Conversation(contact=contact, assigned_user=seller)
            db.add(conversation)
            db.flush()
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body="Oi, quero saber dos planos de internet",
                    status="RECEIVED",
                )
            )
            db.commit()
            conversation_id = conversation.id

        detail = client.get(f"/conversations/{conversation_id}")
        assert DEFAULT_CRM_ORIGIN in detail.text
        registered = client.post(
            f"/conversations/{conversation_id}/register-crm",
            data={
                "csrf_token": _csrf(detail.text),
                "client_name": "Cliente Avulso",
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303
        assert calls[0]["origin"] == DEFAULT_CRM_ORIGIN


def test_origin_campaign_edit_and_delete():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/settings/origin-campaigns")
        assert page.status_code == 200
        created = client.post(
            "/settings/origin-campaigns",
            data={
                "csrf_token": _csrf(page.text),
                "name": "Campanha Editavel",
                "match_text": "Olá, vim do anúncio de CAMPANHA EDITAVEL",
                "crm_origin": "Origem Editavel",
                "link": "",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        with SessionLocal() as db:
            campaign = db.scalar(
                select(OriginCampaign).where(OriginCampaign.name == "Campanha Editavel")
            )
            assert campaign is not None
            campaign_id = campaign.id

        edit_page = client.get(f"/settings/origin-campaigns?edit={campaign_id}")
        assert edit_page.status_code == 200
        assert "Atualizar mapeamento" in edit_page.text
        assert "Campanha Editavel" in edit_page.text
        assert "Editar" in edit_page.text
        assert "Excluir" in edit_page.text

        updated = client.post(
            f"/settings/origin-campaigns/{campaign_id}/edit",
            data={
                "csrf_token": _csrf(edit_page.text),
                "name": "Campanha Editada",
                "match_text": "Olá, vim do anúncio de CAMPANHA EDITADA",
                "crm_origin": "Origem Editada",
                "link": "https://example.com/editada",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303

        with SessionLocal() as db:
            campaign = db.get(OriginCampaign, campaign_id)
            assert campaign is not None
            assert campaign.name == "Campanha Editada"
            assert campaign.crm_origin == "Origem Editada"
            assert campaign.link == "https://example.com/editada"

        list_page = client.get("/settings/origin-campaigns")
        blocked = client.post(
            f"/settings/origin-campaigns/{campaign_id}/delete",
            data={"csrf_token": _csrf(list_page.text), "confirmation": "nao"},
            follow_redirects=True,
        )
        assert blocked.status_code == 200
        assert "Digite EXCLUIR" in blocked.text
        with SessionLocal() as db:
            assert db.get(OriginCampaign, campaign_id) is not None

        deleted = client.post(
            f"/settings/origin-campaigns/{campaign_id}/delete",
            data={"csrf_token": _csrf(list_page.text), "confirmation": "EXCLUIR"},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        with SessionLocal() as db:
            assert db.get(OriginCampaign, campaign_id) is None
