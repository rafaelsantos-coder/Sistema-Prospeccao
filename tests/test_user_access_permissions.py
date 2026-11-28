"""Permissões por usuário: isolamento de menu, operação e rotas diretas."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import ServiceQueue, ServiceQueueMember, User
from app.security import hash_password
from app.services.access_control import (
    ALL_PERMISSION_CODES,
    effective_permissions,
    first_accessible_page,
    has_permission,
    permission_for_request,
    role_default_permissions,
    validate_selection,
)


def fake_user(role="CONSULTOR", codes=None):
    return SimpleNamespace(
        role=role, active=True,
        access_permissions=None if codes is None else json.dumps(codes),
    )


def _csrf(text):
    token = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert token
    return token.group(1)


def _login(client, email, password):
    token = _csrf(client.get("/login").text)
    result = client.post("/login", data={
        "email": email, "password": password, "csrf_token": token,
    }, follow_redirects=False)
    assert result.status_code == 303


def test_legacy_roles_preserve_modules_and_admin_has_everything():
    consultant = fake_user()
    manager = fake_user("GESTOR")
    admin = fake_user("ADMIN", [])
    assert has_permission(consultant, "conversations.view")
    assert not has_permission(consultant, "dispatches.manage")
    assert not has_permission(manager, "voice.view")
    assert has_permission(manager, "dispatches.manage")
    assert all(has_permission(admin, code) for code in ALL_PERMISSION_CODES)
    assert effective_permissions(admin) == ALL_PERMISSION_CODES


def test_explicit_rights_and_parent_dependency_fail_closed():
    user = fake_user(codes=["conversations.view", "crm.register"])
    assert has_permission(user, "crm.register")
    assert not has_permission(user, "leads.view")
    assert not has_permission(user, "crm.credit")
    assert first_accessible_page(user) == "/conversations"
    orphan = fake_user(codes=["voice.campaigns.manage"])
    assert not has_permission(orphan, "voice.campaigns.manage")
    assert effective_permissions(fake_user(codes="not-json")) == frozenset()
    assert not has_permission(fake_user("CONSULTOR", ["settings.users.manage"]), "settings.users.manage")


def test_permission_selection_rejects_escalation_and_missing_parent():
    admin = fake_user("ADMIN")
    manager = fake_user("GESTOR")
    with pytest.raises(HTTPException) as error:
        validate_selection(manager, "CONSULTOR", [
            "voice.view", "voice.accounts.manage",
        ])
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        validate_selection(admin, "CONSULTOR", ["crm.credit"])
    assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        validate_selection(admin, "CONSULTOR", ["unknown.admin", "dashboard.view"])
    assert json.loads(validate_selection(admin, "CONSULTOR", [
        "conversations.view", "crm.credit",
    ])) == ["conversations.view", "crm.credit"]


@pytest.mark.parametrize("path,method,required", [
    ("/conversations", "GET", "conversations.view"),
    ("/api/conversations/inbox", "GET", "conversations.view"),
    ("/conversations/18/register-crm", "POST", "crm.register"),
    ("/conversations/18/consult-cpf", "POST", "crm.credit"),
    ("/conversations/18/refresh-credit-status", "GET", "crm.credit"),
    ("/conversations/18/credit-contestation", "POST", "crm.contest"),
    ("/conversations/18/credit-contestation", "GET", "crm.contest"),
    ("/conversations/18/send", "POST", "conversations.reply"),
    ("/leads/23/whatsapp", "POST", "conversations.reply"),
    ("/leads/new", "GET", "leads.create"),
    ("/leads/23/assign", "POST", "leads.assign"),
    ("/campaigns/1/approve", "POST", "campaigns.approve"),
    ("/dispatches/1/start", "POST", "dispatches.manage"),
    ("/voice/campaigns/new", "GET", "voice.campaigns.manage"),
    ("/voice/campaigns/4", "GET", "voice.view"),
    ("/voice/accounts", "GET", "voice.accounts.manage"),
    ("/traffic", "GET", "traffic.view"),
    ("/traffic/connection", "GET", "traffic.view"),
    ("/traffic/campaigns", "GET", "traffic.view"),
    ("/ai/knowledge", "GET", "ai.view"),
    ("/ai/queues/bind", "POST", "ai.manage"),
    ("/settings/users", "POST", "settings.users.manage"),
    ("/settings/users/11/edit", "GET", "settings.users.manage"),
    ("/api/whatsapp-presence/join", "POST", "conversations.reply"),
    ("/api/reports/credit-map/import", "POST", "credit_map.import"),
    ("/api/integrations/zapi/status", "GET", None),
])
def test_http_routes_are_guarded(path, method, required):
    assert permission_for_request(path, method) == required


def test_admin_can_create_user_with_only_conversations_access():
    email = "permissions.flow.check@sulnet.local"
    password = "SenhaAcesso@2026"
    with TestClient(app) as client:
        _login(client, "admin@sulnet.local", "Sulnet@2026!")
        page = client.get("/settings/users")
        assert page.status_code == 200
        assert "O que este usuário poderá acessar?" in page.text
        assert 'name="permissions"' in page.text
        assert "Prospecta VOZ" in page.text
        assert "Disparos WhatsApp" in page.text
        token = _csrf(page.text)

        response = client.post("/settings/users", data={
            "csrf_token": token,
            "name": "Acesso Personalizado",
            "email": email,
            "password": password,
            "role": "CONSULTOR",
            "team": "LOJA",
            "branch_id": "",
            "permissions_configured": "1",
            "permissions": ["conversations.view", "conversations.reply", "crm.register"],
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            u = db.scalar(select(User).where(User.email == email))
            assert u is not None
            assert u.must_change_password
            assert u.access_permissions is not None
            assert json.loads(u.access_permissions) == [
                "conversations.reply", "conversations.view", "crm.register",
            ]
            u.must_change_password = False
            db.commit()

    with TestClient(app) as client:
        _login(client, email, password)
        dashboard = client.get("/dashboard", follow_redirects=False)
        assert dashboard.status_code == 303
        assert dashboard.headers["location"] == "/conversations"
        assert client.get("/leads", follow_redirects=False).status_code == 403
        assert client.get("/voice", follow_redirects=False).status_code == 403
        assert client.get("/ai", follow_redirects=False).status_code == 403
        assert client.get("/settings/users", follow_redirects=False).status_code == 403
        inbox = client.get("/conversations", follow_redirects=False)
        assert inbox.status_code in {200, 303}  # presence policy still applies
        if inbox.status_code == 200:
            assert "Leads e CRM</span>" not in inbox.text
            assert "Prospecta VOZ</span>" not in inbox.text
            assert "Conversas</span>" in inbox.text


def test_delegated_voice_and_dispatch_access_without_sip_administration():
    email = "delegated.voice.access@sulnet.local"
    password = "VozAutorizada@2026"
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email))
        if u is None:
            u = User(
                name="Operador de Campanhas",
                email=email,
                password_hash=hash_password(password),
                role="CONSULTOR",
                team="LOJA",
                active=True,
                must_change_password=False,
                access_permissions=json.dumps([
                    "dashboard.view",
                    "voice.view", "voice.campaigns.manage",
                    "dispatches.view", "dispatches.manage",
                ]),
            )
            db.add(u)
        else:
            u.must_change_password = False
            u.access_permissions = json.dumps([
                "dashboard.view",
                "voice.view", "voice.campaigns.manage",
                "dispatches.view", "dispatches.manage",
            ])
        db.commit()

    with TestClient(app) as client:
        _login(client, email, password)
        assert client.get("/voice", follow_redirects=False).status_code == 200
        assert client.get("/voice/campaigns/new", follow_redirects=False).status_code == 200
        assert client.get("/dispatches", follow_redirects=False).status_code == 200
        assert client.get("/voice/accounts", follow_redirects=False).status_code == 403
        assert 'href="/voice/lia-voz"' not in client.get("/voice").text
        assert client.get("/voice/lia-voz", follow_redirects=False).status_code == 403
        assert client.get("/ai", follow_redirects=False).status_code == 403
        assert client.get("/settings/users", follow_redirects=False).status_code == 403


def test_manager_cannot_grant_rights_it_does_not_have():
    manager = fake_user("GESTOR")
    with pytest.raises(HTTPException) as error:
        validate_selection(manager, "SUPERVISOR", [
            "conversations.view", "voice.view",
        ])
    assert error.value.status_code == 403


def test_queue_memberships_are_saved_and_can_be_revoked_by_admin():
    """Selecionar a fila no formulário usa a associação real de atendentes."""
    email = "permissions.queue.check@sulnet.local"
    with TestClient(app) as client:
        _login(client, "admin@sulnet.local", "Sulnet@2026!")
        with SessionLocal() as db:
            queue = db.scalar(select(ServiceQueue).where(
                ServiceQueue.code == "TEST-RBAC-Q"
            ))
            if queue is None:
                queue = ServiceQueue(
                    name="Fila Teste Permissão", code="TEST-RBAC-Q",
                    team="LOJA", active=True,
                )
                db.add(queue)
                db.commit()
                db.refresh(queue)
            qid = queue.id
        page = client.get("/settings/users")
        assert "Fila Teste Permissão" in page.text
        assert 'name="queue_ids"' in page.text
        response = client.post("/settings/users", data={
            "csrf_token": _csrf(page.text),
            "name": "Atendente da Fila",
            "email": email,
            "password": "SenhaAcesso@2026",
            "role": "CONSULTOR",
            "team": "LOJA",
            "queue_memberships_configured": "1",
            "queue_ids": [str(qid)],
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user
            uid = user.id
            link = db.scalar(select(ServiceQueueMember).where(
                ServiceQueueMember.user_id == uid,
                ServiceQueueMember.queue_id == qid,
            ))
            assert link is not None and link.active
        detail = client.get(f"/settings/users/{uid}/edit")
        assert detail.status_code == 200
        assert 'value="' + str(qid) + '"' in detail.text
        update = client.post(f"/settings/users/{uid}/edit", data={
            "csrf_token": _csrf(detail.text),
            "name": "Atendente da Fila",
            "email": email,
            "role": "CONSULTOR",
            "team": "LOJA",
            "active": "true",
            "queue_memberships_configured": "1",
            # Nenhuma fila marcada: desativa o vínculo selecionável.
        }, follow_redirects=False)
        assert update.status_code == 303
        with SessionLocal() as db:
            link = db.scalar(select(ServiceQueueMember).where(
                ServiceQueueMember.user_id == uid,
                ServiceQueueMember.queue_id == qid,
            ))
            assert link is not None and link.active is False


def test_roles_only_are_kept_for_old_create_user_post():
    # O novo campo é opcional na API: rotinas/testes antigos continuam
    # criando contas com acesso herdado do perfil, sem mudanças inesperadas.
    u = fake_user("CONSULTOR")
    assert u.access_permissions is None
    assert effective_permissions(u) == role_default_permissions("CONSULTOR")
