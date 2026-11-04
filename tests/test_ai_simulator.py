"""Simulador do SDR: mesma decisão da produção, sem WhatsApp e sem gravar."""

from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.services import ai_sdr


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        follow_redirects=False,
    )
    sim = client.get("/ai/simulator")
    assert sim.status_code == 200
    return re.search(r'data-csrf="([^"]+)"', sim.text).group(1)


def test_simulator_fast_path_for_ad_lead_without_model():
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/ai/simulator/turn",
            json={
                "csrf_token": token,
                "message": "Tenho interesse em assinar 650mb por R$ 99,90",
                "history": [],
                "city": "Santa Rosa",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "650 Mega" in data["reply"]
        assert data["handoff_target"] == "CLOSER_AI"
        assert any("Caminho rápido" in step for step in data["trace"])


def test_simulator_uses_model_decision_and_guardian(monkeypatch):
    decision = ai_sdr.AIDecision(
        reply_text="Boa noite! Você já é cliente Sulnet?",
        intent="QUALIFYING",
        qualification_score=10,
        temperature="FRIO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        confidence=0.9,
        summary="",
        next_action="",
        qualification=ai_sdr.QualificationData(),
    )
    monkeypatch.setattr(ai_sdr, "run_ai_decision", lambda agent, context: decision.model_copy(deep=True))
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/ai/simulator/turn",
            json={"csrf_token": token, "message": "Boa noite", "history": []},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["reply"]
        assert data["guardian"]["verdict"]


def test_simulator_requires_csrf_and_admin():
    with TestClient(app) as client:
        assert client.post("/ai/simulator/turn", json={"message": "oi"}).status_code == 403
        _login(client)
        assert client.post(
            "/ai/simulator/turn", json={"csrf_token": "x", "message": "oi"}
        ).status_code == 403
