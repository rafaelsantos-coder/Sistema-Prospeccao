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
    AuditLog,
    AIConversationState,
    AIJob,
    AIRun,
    Conversation,
    Message,
    ServiceQueue,
    User,
)
from app.services.chat_reports import (
    ChatReportFilters,
    _build_ai_performance_management,
    _build_supervisor_shadow_report,
    build_chat_report,
)
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
        assert "GESTÃO AUTOMÁTICA DE PERFORMANCE" in response.text
        assert "Saúde da jornada" in response.text
        assert "Recomendação principal" in response.text
        assert "SUPERVISOR IA · MODO SOMBRA" in response.text
        assert "Auditoria independente do SDR e do Vendedor IA" in response.text
        assert "Aprovaria" in response.text
        assert "Bloquearia" in response.text


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
        assert "DIAGNÓSTICO AUTOMÁTICO DA IA" in journey_text
        assert "ALERTAS E RECOMENDAÇÕES" in journey_text
        assert "COMPARAÇÃO COM O PERÍODO ANTERIOR" in journey_text

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



def _performance_fixture(
    *,
    ai_conversations=10,
    offer_defined=8,
    purchase_acceptances=4,
    closer_started_period=4,
    completed_period=3,
    human_handoffs=1,
    stalled=0,
    at_risk=0,
    objection_conversations=2,
    acceptance_rate=50.0,
    closer_start_rate=100.0,
    completion_rate=75.0,
    avg_interactions_to_acceptance=3.0,
    bottleneck_code="SELECT_OFFER",
    bottleneck_label="Selecionar oferta",
    bottleneck_current=2,
    stage_avg=1.5,
    stage_stalled=0,
):
    return {
        "kpis": {
            "ai_conversations": ai_conversations,
            "offer_defined": offer_defined,
            "purchase_acceptances": purchase_acceptances,
            "closer_started_period": closer_started_period,
            "completed_period": completed_period,
            "human_handoffs": human_handoffs,
            "stalled": stalled,
            "at_risk": at_risk,
            "objection_conversations": objection_conversations,
            "acceptance_rate": acceptance_rate,
            "closer_start_rate": closer_start_rate,
            "completion_rate": completion_rate,
            "avg_interactions_to_acceptance": avg_interactions_to_acceptance,
            "avg_interactions_per_conversation": 2.0,
            "bottleneck_code": bottleneck_code,
            "bottleneck_label": bottleneck_label,
            "bottleneck_current": bottleneck_current,
        },
        "stages": [
            {
                "code": bottleneck_code,
                "label": bottleneck_label,
                "conversations": 4,
                "interactions": 6,
                "avg_interactions": stage_avg,
                "current": bottleneck_current,
                "stalled": stage_stalled,
            }
        ],
    }


def test_ai_performance_management_detects_conversion_drop_and_stagnation():
    current = _performance_fixture(
        offer_defined=10,
        purchase_acceptances=1,
        acceptance_rate=10.0,
        stalled=3,
        at_risk=4,
        stage_avg=3.2,
        stage_stalled=2,
    )
    previous = _performance_fixture(
        offer_defined=10,
        purchase_acceptances=5,
        acceptance_rate=50.0,
        stalled=0,
        stage_avg=1.4,
    )

    result = _build_ai_performance_management(
        current,
        previous,
        current_label="2026-10-01 a 2026-10-30",
        previous_label="2026-09-01 a 2026-09-30",
    )

    codes = {item["code"] for item in result["alerts"]}
    assert "STALLED_CONVERSATIONS" in codes
    assert "JOURNEY_BOTTLENECK" in codes
    assert "LOW_ACCEPTANCE_RATE" in codes
    assert "ACCEPTANCE_RATE_DROP" in codes
    assert result["critical_count"] >= 2
    assert result["score"] < 70
    assert result["status"] in {"ATTENTION", "CRITICAL"}
    assert result["comparison"]["acceptance_rate_delta"] == -40.0
    assert result["primary_recommendation"]


def test_ai_performance_management_flags_closer_loss_after_acceptance():
    current = _performance_fixture(
        purchase_acceptances=5,
        closer_started_period=2,
        closer_start_rate=40.0,
    )
    previous = _performance_fixture(
        purchase_acceptances=5,
        closer_started_period=5,
        closer_start_rate=100.0,
    )

    result = _build_ai_performance_management(
        current,
        previous,
        current_label="atual",
        previous_label="anterior",
    )

    codes = {item["code"] for item in result["alerts"]}
    assert "CLOSER_HANDOFF_LOSS" in codes
    alert = next(
        item for item in result["alerts"]
        if item["code"] == "CLOSER_HANDOFF_LOSS"
    )
    assert alert["severity"] == "CRITICAL"
    assert "Vendedor IA" in alert["title"]


def test_ai_performance_management_returns_positive_health_when_stable():
    current = _performance_fixture(
        stalled=0,
        at_risk=0,
        stage_avg=1.2,
        bottleneck_current=1,
        human_handoffs=0,
        objection_conversations=1,
    )
    previous = _performance_fixture(
        stalled=0,
        stage_avg=1.3,
        bottleneck_current=1,
        human_handoffs=0,
        objection_conversations=1,
    )

    result = _build_ai_performance_management(
        current,
        previous,
        current_label="atual",
        previous_label="anterior",
    )

    assert result["score"] >= 85
    assert result["status"] == "EXCELLENT"
    assert any(item["code"] == "HEALTHY_JOURNEY" for item in result["alerts"])



def test_supervisor_shadow_report_aggregates_airun_and_followup_reviews(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        location = _create_conversation_with_message(client, monkeypatch, "SUPERVISOR")
        conversation_id = int(location.rsplit("/", 1)[-1])

        db = SessionLocal()
        try:
            db.add_all(
                [
                    AuditLog(
                        action="AI_SUPERVISOR_SHADOW_REVIEW",
                        entity_type="ai_run",
                        entity_id="9001",
                        payload_json=json.dumps(
                            {
                                "conversation_id": conversation_id,
                                "agent_role": "SDR",
                                "verdict": "APPROVE",
                                "risk": "LOW",
                                "confidence": 0.95,
                                "quality_score": 96,
                                "issue_codes": [],
                                "rationale": "Fluxo correto.",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    AuditLog(
                        action="AI_SUPERVISOR_SHADOW_REVIEW",
                        entity_type="followup_audit",
                        entity_id="9002",
                        payload_json=json.dumps(
                            {
                                "conversation_id": conversation_id,
                                "agent_role": "CLOSER",
                                "verdict": "CORRECT",
                                "risk": "HIGH",
                                "confidence": 0.91,
                                "quality_score": 62,
                                "issue_codes": ["MISSING_QUICK_REPLIES"],
                                "rationale": "Pergunta fechada sem botões.",
                                "suggested_reply": "Escolha a forma de pagamento:",
                                "suggested_buttons": ["Boleto", "Débito em conta"],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ]
            )
            db.commit()

            now = utcnow()
            report = _build_supervisor_shadow_report(
                db,
                conversation_ids=[conversation_id],
                start=now.replace(hour=0, minute=0, second=0, microsecond=0),
                end=now.replace(hour=23, minute=59, second=59, microsecond=999999),
            )

            assert report["reviews"] == 2
            assert report["approve"] == 1
            assert report["correct"] == 1
            assert report["block"] == 0
            assert report["approval_rate"] == 50.0
            assert report["intervention_rate"] == 50.0
            assert report["high_risk"] == 1
            assert report["sdr_reviews"] == 1
            assert report["closer_reviews"] == 1
            assert report["top_issues"][0]["code"] == "MISSING_QUICK_REPLIES"
            assert report["recent_findings"][0]["conversation_id"] == conversation_id
        finally:
            db.close()
