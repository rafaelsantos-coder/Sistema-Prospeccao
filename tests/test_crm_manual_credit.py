"""Consulta de CPF de vendedor no CRM, sem IA nem criação duplicada."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Conversation, User
from app.services import crm_manual_credit as credit
from app.routers import conversations as conversation_routes
from app.services import crm_manual_registration as manual


def _csrf(body: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert m
    return m.group(1)


def _login(client: TestClient) -> None:
    token = _csrf(client.get("/login").text)
    r = client.post("/login", data={
        "email": "admin@sulnet.local",
        "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303


def test_cpf_validation_and_decisions():
    assert credit.valid_cpf("111.444.777-35")
    assert credit.valid_cpf("11144477735")
    assert not credit.valid_cpf("111.444.777-34")
    assert not credit.valid_cpf("11111111111")
    assert not credit.valid_cpf("1234")
    assert credit._decision({"status": "Aprovado com taxa", "approved": True}) == (
        "APROVADO_COM_TAXA", "Aprovado com taxa"
    )
    assert credit._decision({"status": "Reprovado", "rejected": True}) == (
        "REPROVADO", "Reprovado"
    )
    assert credit._decision({"status": "Em análise"}) == ("EM_ANALISE", "Em análise")


def test_manual_credit_uses_existing_crm_lead_and_never_saves_cpf(monkeypatch):
    monkeypatch.setattr(
        credit, "settings",
        replace(credit.settings, sulnet_crm_manual_credit_enabled=True),
    )
    monkeypatch.setattr(
        conversation_routes, "settings",
        replace(conversation_routes.settings, sulnet_crm_manual_credit_enabled=True),
    )
    calls = []

    class FakeCRM:
        def consult_seller_credit(self, **kwargs):
            calls.append(kwargs)
            return {
                "opportunityId": 17456,
                "assignedUserId": 24,
                "sellerUsername": "maria.peno",
                "funnelId": 16,
                "result": "Aprovado com taxa",
                "status": "Liberado com taxa",
                "approved": True,
                "analysisId": 991,
                "protocol": "CRED-TEST-991",
            }

    monkeypatch.setattr(
        credit.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM())
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
        url = opened.headers["location"]
        cid = int(url.rstrip("/").split("/")[-1])
        with SessionLocal() as db:
            seller = db.scalar(select(User).where(
                User.email == "maria.credit.manual.test@sulnet.local"
            ))
            if seller is None:
                seller = User(
                    name="Maria Gabriela Paim Peno",
                    email="maria.credit.manual.test@sulnet.local",
                    password_hash="test-hash",
                    role="CONSULTOR",
                    active=True,
                )
                db.add(seller)
                db.flush()
            conversation = db.get(Conversation, cid)
            conversation.assigned_user_id = seller.id
            contact_id = conversation.contact_id
            db.execute(
                delete(AuditLog).where(
                    AuditLog.entity_type == "contact",
                    AuditLog.entity_id == str(contact_id),
                    AuditLog.action.in_((
                        manual.ACTION_REGISTERED, manual.ACTION_UNCERTAIN,
                        manual.ACTION_REVIEW, credit.ACTION_CREDIT,
                        credit.ACTION_UNCERTAIN, credit.ACTION_REVIEW,
                    )),
                )
            )
            db.add(AuditLog(
                action=manual.ACTION_REGISTERED,
                entity_type="contact",
                entity_id=str(contact_id),
                payload_json=json.dumps({
                    "opportunityId": 17456,
                    "sellerUsername": "maria.peno",
                    "protocol": "CRM-2026-17456",
                }),
            ))
            db.add(AuditLog(
                action=credit.ACTION_UNCERTAIN,
                entity_type="contact",
                entity_id=str(contact_id),
                payload_json=json.dumps({
                    "opportunityId": 17455,
                    "sellerUsername": "fabiano.braga",
                }),
            ))
            db.commit()

        page = client.get(url)
        assert "Consulta de CPF" in page.text
        assert 'name="cpf"' in page.text
        assert "Consultar crédito" in page.text
        sent = client.post(
            url + "/consult-cpf",
            data={"csrf_token": _csrf(page.text), "cpf": "111.444.777-35"},
            follow_redirects=False,
        )
        assert sent.status_code == 303
        assert len(calls) == 1
        assert calls[0] == {
            "opportunity_id": 17456,
            "seller_username": "maria.peno",
            "cpf": "11144477735",
        }
        page = client.get(url)
        assert "Aprovado com taxa" in page.text
        again = client.post(
            url + "/consult-cpf",
            data={"csrf_token": _csrf(page.text), "cpf": "111.444.777-35"},
            follow_redirects=False,
        )
        assert again.status_code == 303
        assert len(calls) == 1
        with SessionLocal() as db:
            audits = list(db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "contact",
                    AuditLog.entity_id == str(contact_id),
                    AuditLog.action == credit.ACTION_CREDIT,
                )
            ))
            assert len(audits) == 1
            assert "11144477735" not in audits[0].payload_json
            assert "Aprovado com taxa" in audits[0].payload_json

            # Cada teste utiliza a mesma base SQLite de CI: restaurar o estado
            # para que o teste de registro independente possa criar a lead.
            db.execute(delete(AuditLog).where(
                AuditLog.entity_type == "contact",
                AuditLog.entity_id == str(contact_id),
                AuditLog.action.in_((
                    manual.ACTION_REGISTERED, manual.ACTION_UNCERTAIN,
                    manual.ACTION_REVIEW, credit.ACTION_CREDIT,
                    credit.ACTION_UNCERTAIN, credit.ACTION_REVIEW,
                )),
            ))
            db.commit()


def test_webhook_credit_uses_integration_key_not_sdr_bearer(monkeypatch):
    import app.services.crm_sulnet as crm_module

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "opportunityId": 17456,
                "assignedUserId": 24,
                "status": "Reprovado",
                "rejected": True,
            }

    class FakeClient:
        def __init__(self, *, headers, **kwargs):
            captured["headers"] = headers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, body=kwargs.get("json"))
            return FakeResponse()

    monkeypatch.setattr(crm_module.httpx, "Client", FakeClient)
    api = crm_module.SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="must-not-be-used",
        lead_integration_key="commercial-only-key",
    )
    reply = api.consult_seller_credit(
        opportunity_id=17456,
        seller_username="maria.peno",
        cpf="11144477735",
    )
    assert reply["rejected"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://crm.example/webhook/leads/consult"
    assert captured["body"] == {
        "opportunityId": 17456,
        "sellerUsername": "maria.peno",
        "clientCpf": "11144477735",
    }
    assert captured["headers"]["X-Integration-Key"] == "commercial-only-key"
    assert "Authorization" not in captured["headers"]

    # Em produção, a mesma credencial atual do POST /webhook/leads
    # pode estar configurada como SULNET_AI_INTEGRATION_KEY.
    old_key_client = crm_module.SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="must-not-be-used",
        integration_key="existing-leads-webhook-key",
    )
    old_key_client.consult_seller_credit(
        opportunity_id=17456,
        seller_username="maria.peno",
        cpf="11144477735",
    )
    assert captured["headers"]["X-Integration-Key"] == "existing-leads-webhook-key"
    assert "Authorization" not in captured["headers"]


@pytest.mark.parametrize(
    ("name", "username", "crm_id"),
    [
        ("Gabriele Naiane Barbosa", "gabriele.barbosa", 42),
        ("Michele Zandona", "michele.zandona", 43),
    ],
)
def test_new_santa_rosa_seller_credit_uses_registered_upgrade_funnel(
    monkeypatch, name, username, crm_id,
):
    """Consulta usa sellerUsername oficial e confirma funil 23 da oportunidade."""
    monkeypatch.setattr(
        credit, "settings",
        replace(credit.settings, sulnet_crm_manual_credit_enabled=True),
    )
    registered = {
        "state": manual.ACTION_REGISTERED,
        "opportunityId": 17234,
        "sellerUsername": username,
        "funnelId": 23,
        "stageId": 77,
    }
    calls = []
    audits = []

    class FakeCRM:
        def consult_seller_credit(self, **kwargs):
            calls.append(kwargs)
            return {
                "opportunityId": 17234,
                "assignedUserId": crm_id,
                "sellerUsername": username,
                "funnelId": 23,
                "analysisId": 9876,
                "result": "Em análise",
            }

    monkeypatch.setattr(credit, "_guard_contact_lock", lambda *_: None)
    monkeypatch.setattr(credit, "registration_status", lambda *_: registered)
    monkeypatch.setattr(credit, "credit_status", lambda *_: None)
    monkeypatch.setattr(credit, "_audit", lambda *args, **kwargs: audits.append(kwargs))
    monkeypatch.setattr(
        credit.SulnetCRMClient, "from_settings", classmethod(lambda cls: FakeCRM())
    )
    user = User(name=name, email="vendedor@sulnet.local",
                password_hash="x", active=True, role="CONSULTOR")
    user.id = 88
    conversation = SimpleNamespace(
        id=22, contact_id=33, assigned_user=user, assigned_user_id=user.id
    )
    result = credit.consult_registered_cpf(
        None, conversation=conversation, actor=user, cpf="111.444.777-35"
    )
    assert result["funnelId"] == 23
    assert result["sellerUsername"] == username
    assert calls == [{
        "opportunity_id": 17234,
        "seller_username": username,
        "cpf": "11144477735",
    }]
    assert audits[0]["action"] == credit.ACTION_CREDIT
    assert "cpf" not in str(audits[0]).lower()

