"""RBAC do Prospecta VOZ: somente ADMIN gerencia a telefonia."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models import User, VoiceCampaign, VoiceGroup, VoiceSipAccount
from app.security import hash_password


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "CSRF ausente na pagina"
    return match.group(1)


def _ensure_test_user(role: str) -> tuple[str, str]:
    email = f"voz-rbac-{role.lower()}@sulnet.local"
    password = "ProspectaRole@2026"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            db.add(User(
                name=f"RBAC Voice {role}", email=email,
                password_hash=hash_password(password),
                role=role, team="DIGITAL", active=True,
                must_change_password=False,
            ))
            db.commit()
    return email, password


def _login(client: TestClient, email: str, password: str) -> str:
    csrf = _csrf(client.get("/login").text)
    r = client.post("/login", data={
        "csrf_token": csrf, "email": email, "password": password,
    }, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert 'href="/voice"' not in page.text
    return _csrf(page.text)


@pytest.mark.parametrize("role", ["GESTOR", "SUPERVISOR", "CONSULTOR"])
def test_non_admin_cannot_see_or_mutate_voice(role: str):
    with TestClient(app) as client:
        email, password = _ensure_test_user(role)
        csrf = _login(client, email, password)
        with SessionLocal() as db:
            before = tuple(db.scalar(select(func.count(model.id))) for model in
                           (VoiceSipAccount, VoiceGroup, VoiceCampaign))

        response = client.get("/voice", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"

        operations = [
            ("/voice/accounts", {
                "csrf_token": csrf, "name": "Nao autorizado",
                "domain": "sip.example.com", "username": "200",
                "password": "test-credential",
            }, None),
            ("/voice/groups", {
                "csrf_token": csrf, "name": "Nao autorizado",
                "target_extension": "700",
            }, None),
            ("/voice/campaigns", {
                "csrf_token": csrf, "name": "Nao autorizado",
                "sip_account_id": "1", "group_id": "1", "mode": "DIRECT",
            }, None),
            ("/voice/campaigns/1/review", {"csrf_token": csrf}, None),
            ("/voice/campaigns/1/audio", {"csrf_token": csrf}, {
                "audio": ("audio.wav", b"test", "audio/wav"),
            }),
            ("/voice/campaigns/1/contacts", {"csrf_token": csrf}, {
                "csv_file": ("contatos.csv", b"nome;telefone\nCliente;5555999999999", "text/csv"),
            }),
        ]
        for path, data, files in operations:
            r = client.post(path, data=data, files=files, follow_redirects=False)
            assert r.status_code == 303, (path, r.status_code, r.text[:200])
            assert r.headers["location"] == "/dashboard", path

        with SessionLocal() as db:
            after = tuple(db.scalar(select(func.count(model.id))) for model in
                          (VoiceSipAccount, VoiceGroup, VoiceCampaign))
        assert after == before


def test_only_admin_sees_voice_navigation():
    with TestClient(app) as client:
        token = _csrf(client.get("/login").text)
        result = client.post("/login", data={
            "csrf_token": token,
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
        }, follow_redirects=False)
        assert result.status_code == 303
        page = client.get("/voice")
        assert page.status_code == 200
        assert 'href="/voice"' in page.text
        assert "Somente o administrador" in page.text
