from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Lead,
    ProspectingJob,
    ServiceQueue,
)
from app.routers import prospecting as prospecting_router
from app.services.dispatch_queue import create_dispatch_from_leads
from app.services.queue_routing import bind_prospecting_queue


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/prospecting").text)


def test_prospecting_page_shows_execution_modes():
    with TestClient(app) as client:
        _login_admin(client)
        page = client.get("/prospecting")
        assert page.status_code == 200
        assert "Só buscar leads" in page.text
        assert "Buscar e disparar" in page.text
        assert "Campanha do disparo" in page.text
        assert "Intervalo entre mensagens" in page.text


def test_create_dispatch_from_prospecting_leads_auto_starts():
    with TestClient(app) as client:
        _login_admin(client)
        db = SessionLocal()
        try:
            campaign = Campaign(
                name="Campanha Prospecção Auto",
                channel="WHATSAPP",
                status="APROVADA",
                daily_limit=50,
                team="B2B",
                sequence_json=json.dumps(
                    [{"message": "Olá {{nome}}, a Sulnet tem oferta na sua região."}],
                    ensure_ascii=False,
                ),
            )
            queue = ServiceQueue(
                name="Fila Prospecção Auto",
                code="PROSPECT-AUTO",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            lead = Lead(
                business_name="Clínica Teste",
                phone="5555999887766",
                segment="clinicas",
                city="Santa Rosa",
                source="APIFY",
                team="B2B",
                stage="NOVO",
                score=7,
                temperature="MORNO",
            )
            db.add_all([campaign, queue, lead])
            db.commit()
            db.refresh(campaign)
            db.refresh(queue)
            db.refresh(lead)

            dispatch, summary = create_dispatch_from_leads(
                db,
                campaign=campaign,
                queue=queue,
                leads=[lead],
                created_by_id=None,
                interval_seconds=20,
                daily_limit=40,
                auto_start=True,
                source="PROSPECTING",
            )
            db.commit()

            assert dispatch is not None
            assert dispatch.status == "ATIVO"
            assert dispatch.interval_seconds == 20
            assert dispatch.daily_limit == 40
            assert summary["imported"] == 1
            recipients = list(
                db.scalars(
                    select(CampaignDispatchRecipient).where(
                        CampaignDispatchRecipient.dispatch_id == dispatch.id
                    )
                )
            )
            assert len(recipients) == 1
            assert recipients[0].phone == "5555999887766"
            assert "Clínica Teste" in recipients[0].message_body
        finally:
            db.close()


def test_prospecting_job_search_and_dispatch_creates_dispatch(monkeypatch):
    with TestClient(app) as client:
        _login_admin(client)
        db = SessionLocal()
        try:
            campaign = Campaign(
                name="Campanha Job Disparo",
                channel="WHATSAPP",
                status="APROVADA",
                daily_limit=80,
                team="B2B",
                sequence_json=json.dumps(
                    [{"message": "Oi {{nome}}! Posso te mostrar os planos?"}],
                    ensure_ascii=False,
                ),
            )
            queue = ServiceQueue(
                name="Fila Job Disparo",
                code="PROSPECT-JOB",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add_all([campaign, queue])
            db.commit()
            db.refresh(campaign)
            db.refresh(queue)

            async def fake_search(self, *, segment, city, limit):
                return [
                    {
                        "business_name": "Mercado Central",
                        "phone": "5555912345678",
                        "email": "mercado@example.com",
                        "website": "",
                        "segment": segment,
                        "city": city,
                        "address": "Rua 1",
                        "score": 8,
                        "legacy_score": 8,
                        "temperature": "QUENTE",
                        "potential": "ALTO",
                        "raw_data": {},
                    }
                ]

            monkeypatch.setattr(
                prospecting_router.ApifyService,
                "search",
                fake_search,
            )

            job = ProspectingJob(
                segment="mercados",
                city="Santa Rosa",
                limit=10,
                team="B2B",
                status="PENDENTE",
                execution_json=json.dumps(
                    {
                        "mode": "SEARCH_AND_DISPATCH",
                        "campaign_id": campaign.id,
                        "campaign_name": campaign.name,
                        "queue_id": queue.id,
                        "interval_seconds": 15,
                        "daily_limit": 25,
                        "auto_start": True,
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(job)
            db.commit()
            job_id = job.id
            bind_prospecting_queue(db, job_id, queue.id)
            db.commit()

            prospecting_router._run_job(job_id)

            db.expire_all()
            job = db.get(ProspectingJob, job_id)
            assert job is not None
            assert job.status == "CONCLUIDO"
            assert job.imported_count == 1
            plan = json.loads(job.execution_json)
            assert plan["mode"] == "SEARCH_AND_DISPATCH"
            assert plan.get("dispatch_id")
            dispatch = db.get(CampaignDispatch, plan["dispatch_id"])
            assert dispatch is not None
            assert dispatch.status == "ATIVO"
            assert dispatch.interval_seconds == 15
        finally:
            db.close()


def test_create_job_form_persists_execution_plan(monkeypatch):
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign = Campaign(
                name="Campanha Form",
                channel="WHATSAPP",
                status="APROVADA",
                daily_limit=30,
                team="B2B",
                sequence_json=json.dumps(
                    [{"message": "Olá {{nome}}"}],
                    ensure_ascii=False,
                ),
            )
            queue = ServiceQueue(
                name="Fila Form",
                code="PROSPECT-FORM",
                team="B2B",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add_all([campaign, queue])
            db.commit()
            db.refresh(campaign)
            db.refresh(queue)
            campaign_id = campaign.id
            queue_id = queue.id
        finally:
            db.close()

        monkeypatch.setattr(prospecting_router, "_run_job", lambda _job_id: None)

        response = client.post(
            "/prospecting",
            data={
                "csrf_token": token,
                "segment": "hotéis",
                "city": "Santa Rosa",
                "limit": "12",
                "team": "B2B",
                "queue_id": str(queue_id),
                "execution_mode": "SEARCH_AND_DISPATCH",
                "campaign_id": str(campaign_id),
                "interval_seconds": "45",
                "daily_limit": "60",
                "auto_start_dispatch": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            job = db.scalar(
                select(ProspectingJob).order_by(ProspectingJob.id.desc()).limit(1)
            )
            assert job is not None
            plan = json.loads(job.execution_json)
            assert plan["mode"] == "SEARCH_AND_DISPATCH"
            assert plan["campaign_id"] == campaign_id
            assert plan["queue_id"] == queue_id
            assert plan["interval_seconds"] == 45
            assert plan["daily_limit"] == 60
            assert plan["auto_start"] is True
        finally:
            db.close()
