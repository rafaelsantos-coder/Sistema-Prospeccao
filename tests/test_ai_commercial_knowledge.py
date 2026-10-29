from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AIAgentProfile, AIKnowledgeItem, AIKnowledgeMetadata
from app.services.ai_sdr import retrieve_knowledge
from app.services.seed import seed_database
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": _csrf(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _sulia(db):
    agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.slug == "sulia-sdr-digital")
    )
    assert agent is not None
    return agent


def test_seed_contains_value_first_sulnet_knowledge():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _sulia(db)
            item = db.scalar(
                select(AIKnowledgeItem).where(
                    AIKnowledgeItem.agent_id == agent.id,
                    AIKnowledgeItem.title == "Por que contratar a Sulnet - vender valor antes de preço",
                )
            )
            assert item is not None
            assert item.category == "ARGUMENTOS_COMERCIAIS"
            assert "não iniciar a resposta pelo preço" in item.content.lower()

            meta = db.scalar(
                select(AIKnowledgeMetadata).where(
                    AIKnowledgeMetadata.knowledge_item_id == item.id
                )
            )
            assert meta is not None
            assert meta.approval_status == "APPROVED"
            assert meta.priority == 100
        finally:
            db.close()


def test_why_sulnet_retrieval_prioritizes_value_argument():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _sulia(db)
            items = retrieve_knowledge(
                db,
                agent=agent,
                query="Por que eu deveria contratar a Sulnet? Qual o diferencial?",
            )
            assert items
            assert items[0].title == "Por que contratar a Sulnet - vender valor antes de preço"
        finally:
            db.close()


def test_retrieval_ignores_draft_and_expired_knowledge():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _sulia(db)
            draft = AIKnowledgeItem(
                agent_id=agent.id,
                title="Zeta Draft Nunca Usar",
                category="DIFERENCIAIS",
                tags="zeta-draft-exclusivo",
                content="zeta-draft-exclusivo promessa não aprovada",
                source_type="MANUAL",
                active=True,
            )
            expired = AIKnowledgeItem(
                agent_id=agent.id,
                title="Omega Expirado Nunca Usar",
                category="DIFERENCIAIS",
                tags="omega-expirado-exclusivo",
                content="omega-expirado-exclusivo conteúdo vencido",
                source_type="MANUAL",
                active=True,
            )
            db.add_all([draft, expired])
            db.flush()
            db.add_all(
                [
                    AIKnowledgeMetadata(
                        knowledge_item_id=draft.id,
                        subject="Teste draft",
                        priority=100,
                        approval_status="DRAFT",
                    ),
                    AIKnowledgeMetadata(
                        knowledge_item_id=expired.id,
                        subject="Teste expirado",
                        priority=100,
                        approval_status="APPROVED",
                        valid_until=utcnow() - timedelta(days=1),
                    ),
                ]
            )
            db.commit()

            titles = {
                item.title
                for item in retrieve_knowledge(
                    db,
                    agent=agent,
                    query="zeta draft exclusivo omega expirado exclusivo",
                )
            }
            assert draft.title not in titles
            assert expired.title not in titles

            db.delete(db.get(AIKnowledgeMetadata, draft.id))
        finally:
            # Test DB is shared; remove the temporary rows explicitly.
            draft_row = db.scalar(
                select(AIKnowledgeItem).where(
                    AIKnowledgeItem.title == "Zeta Draft Nunca Usar"
                )
            )
            expired_row = db.scalar(
                select(AIKnowledgeItem).where(
                    AIKnowledgeItem.title == "Omega Expirado Nunca Usar"
                )
            )
            for row in (draft_row, expired_row):
                if row:
                    meta = db.scalar(
                        select(AIKnowledgeMetadata).where(
                            AIKnowledgeMetadata.knowledge_item_id == row.id
                        )
                    )
                    if meta:
                        db.delete(meta)
                    db.delete(row)
            db.commit()
            db.close()


def test_v2_seed_preserves_admin_edits():
    with TestClient(app):
        db = SessionLocal()
        try:
            agent = _sulia(db)
            item = db.scalar(
                select(AIKnowledgeItem).where(
                    AIKnowledgeItem.agent_id == agent.id,
                    AIKnowledgeItem.title == "Diferenciais competitivos e cultura Sulnet",
                )
            )
            assert item is not None
            assert item.source_type == "SULNET_KB_V2"
            original = item.content
            item.content = "EDIÇÃO ADMINISTRATIVA PRESERVADA"
            db.commit()

            seed_database(db, include_demo=False)
            db.refresh(item)
            assert item.content == "EDIÇÃO ADMINISTRATIVA PRESERVADA"

            item.content = original
            db.commit()
        finally:
            db.close()


def test_knowledge_dashboard_is_available_with_training_sections():
    with TestClient(app) as client:
        _login_admin(client)
        page = client.get("/ai/knowledge")
        assert page.status_code == 200
        assert "Conhecimento Sulnet" in page.text
        assert "Regra de ouro da marca" in page.text
        assert "Institucional e história da empresa" in page.text
        assert "Diferenciais competitivos" in page.text
        assert "Argumentos comerciais" in page.text
        assert "Perguntas sem resposta segura" in page.text
