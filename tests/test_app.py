from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token não encontrado"
    return match.group(1)


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_login_and_dashboard():
    with TestClient(app) as client:
        page = client.get("/login")
        token = csrf_from(page.text)
        response = client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "O que precisa avançar hoje" in dashboard.text
        assert "Clínica Horizonte" in dashboard.text


def test_create_lead_flow():
    with TestClient(app) as client:
        page = client.get("/login")
        token = csrf_from(page.text)
        client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        )
        form = client.get("/leads/new")
        token = csrf_from(form.text)
        response = client.post(
            "/leads/new",
            data={
                "csrf_token": token,
                "business_name": "Empresa Teste Automatizado",
                "contact_name": "João",
                "phone": "(55) 99999-7788",
                "email": "joao.teste@example.com",
                "segment": "Empresa de tecnologia",
                "city": "Santa Rosa",
                "source": "MANUAL",
                "origin_channel": "WHATSAPP",
                "team": "B2B",
                "stage": "NOVO",
                "priority": "NORMAL",
                "estimated_mrr": "299,90",
                "notes": "Criado pelo teste.",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/leads/")
        detail = client.get(response.headers["location"])
        assert detail.status_code == 200
        assert "Empresa Teste Automatizado" in detail.text
        assert "299,90" in detail.text
