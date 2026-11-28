"""Navigation, CRUD and RBAC regression tests for the Prospecta VOZ admin module."""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import User, VoiceCampaign, VoiceGroup, VoiceSipAccount
from app.security import hash_password
from app.services.secret_store import decrypt_secret


def _csrf(content: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', content)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "csrf_token": token, "email": "admin@sulnet.local", "password": "Sulnet@2026!",
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice").text)


def test_admin_dashboard_and_all_submenus():
    with TestClient(app) as client:
        _login_admin(client)
        dashboard = client.get("/voice")
        assert dashboard.status_code == 200
        assert "Dashboard Prospecta VOZ" in dashboard.text
        assert "Somente o administrador" in dashboard.text
        for path in ("/voice/accounts", "/voice/groups", "/voice/campaigns/new", "/voice/campaigns"):
            assert f'href="{path}"' in dashboard.text
            response = client.get(path)
            assert response.status_code == 200, path
            assert "Prospecta VOZ" in response.text
            assert 'class="nav-submenu"' in response.text
        assert "Não verificado" in dashboard.text or "Nenhuma conta SIP" in dashboard.text
        assert "HOMOLOGAÇÃO SIP PENDENTE" in dashboard.text
        assert re.search(r'<a href="/voice/lia-voz" class="[^"]*">Eduarda Voz — SDRIA</a>', dashboard.text)
        lia = client.get("/voice/lia-voz", follow_redirects=False)
        assert lia.status_code == 200
        assert '<a href="/voice/lia-voz" class="active">Eduarda Voz — SDRIA</a>' in lia.text
        assert "Conversar com Eduarda Voz" in lia.text


def test_account_and_group_edit_pause_resume_and_safe_delete():
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        token = _login_admin(client)
        account_name = f"Conta VOZ {suffix}"
        group_name = f"Grupo VOZ {suffix}"

        response = client.post("/voice/accounts", data={
            "csrf_token": token, "name": account_name,
            "domain": "sip.example.com", "username": "88003",
            "password": "test-credential-unique", "transport": "UDP",
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/voice/accounts"
        response = client.post("/voice/groups", data={
            "csrf_token": token, "name": group_name,
            "target_extension": "700", "member_extensions": "201, 202",
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/voice/groups"

        with SessionLocal() as db:
            account = db.scalar(select(VoiceSipAccount).where(VoiceSipAccount.name == account_name))
            group = db.scalar(select(VoiceGroup).where(VoiceGroup.name == group_name))
            assert account and group
            account_id, group_id = account.id, group.id
            assert account.password_encrypted != "test-credential-unique"
            assert decrypt_secret(account.password_encrypted) == "test-credential-unique"

        page = client.get(f"/voice/accounts?edit_id={account_id}")
        assert page.status_code == 200
        assert 'Deixe em branco para manter a senha atual' in page.text
        assert "test-credential-unique" not in page.text

        response = client.post(f"/voice/accounts/{account_id}/update", data={
            "csrf_token": token, "name": account_name+" Alterada",
            "domain": "SIP.NEW.EXAMPLE", "username": "88004", "transport": "TCP",
            "password": "",
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            account = db.get(VoiceSipAccount, account_id)
            assert account and account.name == account_name+" Alterada"
            assert account.domain == "sip.new.example"
            assert account.username == "88004"
            assert account.transport == "TCP"
            assert decrypt_secret(account.password_encrypted) == "test-credential-unique"

        response = client.post(f"/voice/accounts/{account_id}/toggle",
                               data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceSipAccount, account_id).active is False
        response = client.post(f"/voice/accounts/{account_id}/toggle",
                               data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceSipAccount, account_id).active is True

        response = client.post(f"/voice/groups/{group_id}/update", data={
            "csrf_token": token, "name": group_name + " Alterado",
            "target_extension": "799", "member_extensions": "205, 206",
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            group = db.get(VoiceGroup, group_id)
            assert group and group.target_extension == "799"
            assert group.member_extensions == "205,206"

        assert client.get(f"/voice/accounts/{account_id}/delete").status_code == 200
        wrong = client.post(f"/voice/accounts/{account_id}/delete", data={
            "csrf_token": token, "confirm_value": "wrong"
        }, follow_redirects=False)
        assert wrong.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceSipAccount, account_id) is not None

        good = client.post(f"/voice/accounts/{account_id}/delete", data={
            "csrf_token": token, "confirm_value": account_name+" Alterada"
        }, follow_redirects=False)
        assert good.status_code == 303
        good = client.post(f"/voice/groups/{group_id}/delete", data={
            "csrf_token": token, "confirm_value": group_name+" Alterado"
        }, follow_redirects=False)
        assert good.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceSipAccount, account_id) is None
            assert db.get(VoiceGroup, group_id) is None


def test_cannot_delete_account_or_group_linked_to_campaign():
    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        token = _login_admin(client)
        with SessionLocal() as db:
            a = VoiceSipAccount(
                name=f"Vinculada {suffix}", domain="sip.example.com", username="305",
                password_encrypted="encrypted-placeholder", transport="UDP", active=True,
            )
            g = VoiceGroup(name=f"Vinculado {suffix}", target_extension="701",
                           member_extensions="", active=True)
            db.add_all([a, g])
            db.flush()
            campaign = VoiceCampaign(name=f"Voice Linked {suffix}", mode="DIRECT",
                                     sip_account_id=a.id, group_id=g.id, status="RASCUNHO",
                                     audio_uri="", dtmf_digit="1", daily_limit=30,
                                     interval_seconds=30, window_start="09:00",
                                     window_end="18:00", legal_basis_verified=False)
            db.add(campaign)
            db.commit()
            a_id, g_id = a.id, g.id

        for kind, number, name in (
            ("accounts", a_id, f"Vinculada {suffix}"),
            ("groups", g_id, f"Vinculado {suffix}"),
        ):
            page = client.get(f"/voice/{kind}/{number}/delete")
            assert page.status_code == 200
            assert "Exclusão indisponível" in page.text
            response = client.post(f"/voice/{kind}/{number}/delete", data={
                "csrf_token": token, "confirm_value": name
            }, follow_redirects=False)
            assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceSipAccount, a_id) is not None
            assert db.get(VoiceGroup, g_id) is not None


def test_non_admin_has_no_access_to_new_submenus_and_actions():
    suffix = uuid4().hex[:8]
    email = f"voz-nav-consultor-{suffix}@sulnet.local"
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(User(
                name="Voice No Access", email=email, role="CONSULTOR",
                team="DIGITAL", active=True, must_change_password=False,
                password_hash=hash_password("TestUser@2026!"),
            ))
            db.commit()
        token = _csrf(client.get("/login").text)
        result = client.post("/login", data={
            "email": email, "password": "TestUser@2026!", "csrf_token": token,
        }, follow_redirects=False)
        assert result.status_code == 303
        csrf = _csrf(client.get("/dashboard").text)
        for path in ("/voice", "/voice/accounts", "/voice/groups", "/voice/campaigns",
                     "/voice/campaigns/new", "/voice/accounts/1/delete",
                     "/voice/groups/1/delete"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/dashboard", path
        for path, data in (
            ("/voice/accounts/1/toggle", {}),
            ("/voice/accounts/1/update", {
                "name": "hijack", "domain": "sip.example.com", "username": "200"
            }),
            ("/voice/accounts/1/delete", {"confirm_value": "x"}),
            ("/voice/groups/1/toggle", {}),
            ("/voice/groups/1/update", {"name": "hijack", "target_extension": "201"}),
            ("/voice/groups/1/delete", {"confirm_value": "x"}),
        ):
            r = client.post(path, data={"csrf_token": csrf, **data}, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/dashboard", path
