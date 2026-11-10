"""Nota automática por conversa e painel de qualidade."""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.services.ai_quality import score_conversation
from app.timeutils import utcnow


def _msg(direction: str, body: str, minutes_ago: int):
    return SimpleNamespace(direction=direction, body=body, created_at=utcnow() - timedelta(minutes=minutes_ago))


def _conversation(stage: str = "NOVO"):
    return SimpleNamespace(
        id=239,
        status="AGUARDANDO_CLIENTE",
        lead=SimpleNamespace(stage=stage),
        contact=SimpleNamespace(business_name="", name="Cliente", phone="5555"),
    )


def test_repetition_frustration_and_silence_lower_the_score():
    question = "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"
    messages = [
        _msg("INBOUND", "Tenho interesse no 650", 3000),
        _msg("OUTBOUND", question, 2990),
        _msg("INBOUND", "Não", 2980),
        _msg("OUTBOUND", question, 2970),
        _msg("INBOUND", "Está fazendo perguntas demais", 2960),
        _msg("OUTBOUND", question, 2950),
        _msg("OUTBOUND", "Oi, ainda está por aí?", 2900),
    ]
    runs = [
        SimpleNamespace(status="FALHOU", error="Guardião bloqueou a decisão antes do envio: X"),
        SimpleNamespace(status="CONCLUIDO", error=None),
    ]

    quality = score_conversation(
        _conversation(), messages, runs, closing=None, ai_state=None, followups_sent=1,
    )

    assert quality.repeated_texts == 2
    assert quality.guardian_blocks == 1
    assert quality.frustration == ["perguntas demais"]
    assert quality.outcome == "SILENCIO"
    assert quality.stuck is True
    assert quality.score == 100 - 20 - 10 - 20
    assert any("repetido" in flag for flag in quality.flags)


def test_won_conversation_without_problems_scores_high():
    messages = [_msg("INBOUND", "quero contratar", 60), _msg("OUTBOUND", "Contrato assinado com sucesso!", 50)]
    quality = score_conversation(
        _conversation(stage="GANHO"), messages, [], closing=None, ai_state=None, followups_sent=0,
    )
    assert quality.outcome == "GANHO"
    assert quality.score == 100
    assert quality.flags == []


def test_guardian_observation_summary_counts_shadow_would_intervene():
    import json

    from app.database import SessionLocal
    from app.models import AuditLog
    from app.services.ai_quality import guardian_observation_summary

    db = SessionLocal()
    try:
        conversation_id = 91001
        db.add(
            AuditLog(
                action="AI_SUPERVISOR_GUARDIAN_REVIEW",
                entity_type="conversation",
                entity_id=str(conversation_id),
                payload_json=json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "verdict": "APPROVE",
                        "observed_verdict": "REWRITE",
                        "shadow_only": True,
                        "would_intervene": True,
                        "issue_codes": ["MISSING_QUICK_REPLIES"],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
        summary = guardian_observation_summary(
            db, days=7, conversation_ids=[conversation_id]
        )
        assert summary["shadow_reviews"] == 1
        assert summary["enforce_reviews"] == 0
        assert summary["would_intervene"] == 1
        assert summary["would_intervene_rate"] == 100
        assert summary["top_issues"][0]["code"] == "MISSING_QUICK_REPLIES"
    finally:
        db.close()


def test_quality_page_renders_for_admin():
    with TestClient(app) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
            follow_redirects=False,
        )
        response = client.get("/ai/quality?days=7")
        assert response.status_code == 200
        assert "Qualidade das conversas" in response.text


def test_case_export_is_anonymized_and_admin_only():
    from app.database import SessionLocal
    from app.models import Contact, Conversation, Message

    with TestClient(app) as client:
        db = SessionLocal()
        try:
            contact = Contact(phone="5555900000911", name="Cliente Caso")
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, status="EM_FILA", team="DIGITAL")
            db.add(conversation)
            db.flush()
            db.add(Message(
                conversation_id=conversation.id,
                contact_id=contact.id,
                direction="INBOUND",
                message_type="TEXT",
                body="Meu CPF é 123.456.789-09 e meu e-mail cliente@exemplo.com",
                status="RECEIVED",
            ))
            db.commit()
            conversation_id = conversation.id
        finally:
            db.close()

        assert client.get(f"/ai/quality/{conversation_id}/case.json", follow_redirects=False).status_code == 303

        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
            follow_redirects=False,
        )
        response = client.get(f"/ai/quality/{conversation_id}/case.json")
        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "conversation_replay"
        text = body["input"]["history"][0]["body"]
        assert "123.456.789-09" not in text
        assert "cliente@exemplo.com" not in text
