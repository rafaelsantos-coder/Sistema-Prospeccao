from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app
from app.services.zapi import ZApiService


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

        messages = client.get("/reports/whatsapp/export/messages.csv")
        assert messages.status_code == 200
        message_text = messages.content.decode("utf-8-sig")
        assert "Mensagem para relatório de atendimento." in message_text

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
