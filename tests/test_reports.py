from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIJob,
    AIRun,
    Conversation,
    Message,
    ServiceQueue,
    User,
)
from app.services.chat_reports import ChatReportFilters, build_chat_report
from app.services.zapi import ZApiService
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
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


def _create_conversation_with_message(client: TestClient, monkeypatch, suffix: str) -> str:
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": f"TEST-REPORT-MSG-{suffix}",
            "zaapId": f"TEST-REPORT-ZAAP-{suffix}",
        },
    )
    lead = client.get("/leads/1")
    token = _csrf(lead.text)
    opened = client.post(
        "/leads/1/conversation",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    location = opened.headers["location"]
    inbox = client.get(location)
    token = _csrf(inbox.text)
    sent = client.post(
        f"{location}/send",
        data={
            "csrf_token": token,
            "message": "Mensagem para relatório de atendimento.",
        },
        follow_redirects=False,
    )
    assert sent.status_code == 303
    return location


def test_whatsapp_reports_page(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        _create_conversation_with_message(client, monkeypatch, "PAGE")
        response = client.get("/reports/whatsapp")
        assert response.status_code == 200
        assert "Relatórios WhatsApp" in response.text
        assert "Operação WhatsApp" in response.text
        assert "Mensagem para relatório de atendimento." in response.text
        assert "PDF gerencial" in response.text
        assert "CSV mensagens" in response.text
        assert 'name="queue_id"' in response.text
        assert "<span>Fila</span>" in response.text
        assert "JORNADA COMERCIAL IA" in response.text
        assert "EFICIÊNCIA POR ETAPA" in response.text
        assert "CSV jornada IA" in response.text
        assert "Interações até aceite" in response.text


def test_whatsapp_reports_csv_exports(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        location = _create_conversation_with_message(client, monkeypatch, "CSV")

        summary = client.get("/reports/whatsapp/export/conversations.csv")
        assert summary.status_code == 200
        assert "text/csv" in summary.headers["content-type"]
        text = summary.content.decode("utf-8-sig")
        assert "Conversa;Contato;Telefone" in text
        assert "Clínica Horizonte" in text
        assert "Jornada IA" in text
        assert "Pendência da jornada" in text
        assert "Estagnação" in text

        messages = client.get("/reports/whatsapp/export/messages.csv")
        assert messages.status_code == 200
        message_text = messages.content.decode("utf-8-sig")
        assert "Mensagem para relatório de atendimento." in message_text

        journey = client.get("/reports/whatsapp/export/ai-journey.csv")
        assert journey.status_code == 200
        journey_text = journey.content.decode("utf-8-sig")
        assert "FUNIL COMERCIAL IA" in journey_text
        assert "DESEMPENHO POR ETAPA" in journey_text
        assert "CONVERSAS TRAVADAS" in journey_text
        assert "Confirmar compra" in journey_text

        conversation_id = location.rsplit("/", 1)[-1]
        transcript = client.get(f"/reports/whatsapp/conversations/{conversation_id}.csv")
        assert transcript.status_code == 200
        transcript_text = transcript.content.decode("utf-8-sig")
        assert "Mensagem para relatório de atendimento." in transcript_text


def test_whatsapp_reports_pdf_exports(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        location = _create_conversation_with_message(client, monkeypatch, "PDF")

        report = client.get("/reports/whatsapp/export/report.pdf")
        assert report.status_code == 200
        assert report.headers["content-type"] == "application/pdf"
        assert report.content.startswith(b"%PDF")

        conversation_id = location.rsplit("/", 1)[-1]
        transcript = client.get(f"/reports/whatsapp/conversations/{conversation_id}.pdf")
        assert transcript.status_code == 200
        assert transcript.headers["content-type"] == "application/pdf"
        assert transcript.content.startswith(b"%PDF")


def test_whatsapp_reports_requires_login():
    with TestClient(app) as client:
        response = client.get("/reports/whatsapp", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"



def test_ai_journey_report_counts_acceptance_and_completed_sale(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        location = _create_conversation_with_message(client, monkeypatch, "AI-JOURNEY")
        conversation_id = int(location.rsplit("/", 1)[-1])

        db = SessionLocal()
        try:
            conversation = db.get(Conversation, conversation_id)
            assert conversation is not None
            message = db.scalar(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(1)
            )
            assert message is not None

            agent = db.scalar(
                select(AIAgentProfile)
                .where(AIAgentProfile.role_type == "SDR")
                .order_by(AIAgentProfile.id.asc())
                .limit(1)
            )
            assert agent is not None
            queue = db.scalar(select(ServiceQueue).order_by(ServiceQueue.id.asc()).limit(1))
            assert queue is not None

            job = AIJob(
                agent_id=agent.id,
                queue_id=queue.id,
                conversation_id=conversation_id,
                message_id=message.id,
                status="CONCLUIDO",
                available_at=utcnow(),
            )
            db.add(job)
            db.flush()

            run = AIRun(
                job_id=job.id,
                agent_id=agent.id,
                conversation_id=conversation_id,
                message_id=message.id,
                execution_mode="AUTO",
                model="gpt-6-luna",
                status="CONCLUIDO",
                intent="HIGH_INTENT",
                qualification_score=100,
                temperature="QUENTE",
                confidence=0.99,
                handoff_recommended=False,
                reply_text="",
                summary="Cliente aceitou a oferta.",
                input_snapshot_json="{}",
                output_json=json.dumps(
                    {
                        "journey_state": "CLOSING",
                        "journey_pending_action": "Iniciar Vendedor IA.",
                        "handoff_target": "CLOSER_AI",
                        "offer_plan_code": "FTTH_800",
                        "offer_price": 119.90,
                        "objection": "NONE",
                        "stagnation_count": 0,
                    },
                    ensure_ascii=False,
                ),
                started_at=utcnow(),
                finished_at=utcnow(),
                created_at=utcnow(),
            )
            db.add(run)
            db.flush()

            ai_state = db.scalar(
                select(AIConversationState).where(
                    AIConversationState.conversation_id == conversation_id
                )
            )
            if not ai_state:
                ai_state = AIConversationState(
                    conversation_id=conversation_id,
                    agent_id=agent.id,
                    state="COMPLETED",
                    summary="Venda concluída em teste.",
                    qualification_score=100,
                    temperature="QUENTE",
                    last_intent="PURCHASE_INTENT",
                    last_run_id=run.id,
                )
                db.add(ai_state)
            else:
                ai_state.agent_id = agent.id
                ai_state.state = "COMPLETED"
                ai_state.last_run_id = run.id

            closing = db.scalar(
                select(AIClosingState).where(
                    AIClosingState.conversation_id == conversation_id
                )
            )
            if not closing:
                closing = AIClosingState(
                    conversation_id=conversation_id,
                    agent_id=agent.id,
                    external_id=f"report-test-{conversation_id}",
                    step="COMPLETED",
                    plan_label="800 Mega",
                    offer_price=119.90,
                    completed_at=utcnow(),
                )
                db.add(closing)
            else:
                closing.step = "COMPLETED"
                closing.completed_at = utcnow()

            db.commit()

            admin = db.scalar(
                select(User).where(User.email == "admin@sulnet.local")
            )
            assert admin is not None
            report = build_chat_report(
                db,
                admin,
                ChatReportFilters.defaults(),
            )
            journey = report["ai_journey"]
            assert journey["kpis"]["ai_conversations"] >= 1
            assert journey["kpis"]["offer_defined"] >= 1
            assert journey["kpis"]["purchase_acceptances"] >= 1
            assert journey["kpis"]["closer_started_period"] >= 1
            assert journey["kpis"]["completed_period"] >= 1
            assert any(
                item["label"] == "Aceite explícito" and item["value"] >= 1
                for item in journey["funnel"]
            )
        finally:
            db.close()
