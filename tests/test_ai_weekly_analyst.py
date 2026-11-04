"""Analista semanal: causas de falha, rascunhos do manual e página."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AIAgentProfile, AIKnowledgeItem, AIKnowledgeMetadata
from app.services.ai_sdr_tools import load_manual
from app.services.ai_weekly_analyst import draft_manual_gaps, failure_cause, manual_versions


def test_failure_causes_are_grouped():
    assert failure_cause("Guardião bloqueou a decisão antes do envio: REPEATED_QUESTION") == (
        "Guardião bloqueou (REPEATED_QUESTION)"
    )
    assert failure_cause("Integridade da jornada bloqueou a decisão antes do envio: STATE_REGRESSION_CITY").startswith(
        "Integridade da jornada"
    )
    assert failure_cause('crm_http_failed status=404 body={"error": "Rota não encontrada."}') == "CRM: rota inexistente"
    assert failure_cause("Etapa GANHO não encontrada no funil") == "CRM: etapa GANHO"
    assert failure_cause(None) == "Sem detalhe"


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        follow_redirects=False,
    )
    weekly = client.get("/ai/weekly")
    assert weekly.status_code == 200
    assert "Analista semanal" in weekly.text
    return re.search(r'name="csrf_token" value="([^"]+)"', weekly.text).group(1)


def test_unanswered_questions_become_review_drafts_not_used_by_ai():
    with TestClient(app):
        db = SessionLocal()
        try:
            report = {"unanswered": [("Vocês instalam em área rural?", 3)]}
            created = draft_manual_gaps(db, report)
            db.commit()
            assert len(created) == 1
            assert draft_manual_gaps(db, report) == []

            item = created[0]
            meta = db.scalar(
                select(AIKnowledgeMetadata).where(AIKnowledgeMetadata.knowledge_item_id == item.id)
            )
            assert meta.approval_status == "REVIEW"

            sdr = db.get(AIAgentProfile, item.agent_id)
            titles = {row["title"] for row in load_manual(db, sdr)}
            assert item.title not in titles

            versions = {row["agent"]: row for row in manual_versions(db)}
            assert versions[sdr.name]["review"] >= 1
            assert versions[sdr.name]["version"].startswith("v")
        finally:
            db.close()


def test_weekly_page_and_manual_run():
    with TestClient(app) as client:
        token = _login(client)
        response = client.post("/ai/weekly/run", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/ai/weekly")
        assert "Última execução" in page.text
