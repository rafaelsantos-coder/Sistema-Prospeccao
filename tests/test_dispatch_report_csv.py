from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from secrets import token_urlsafe

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Campaign, CampaignDispatch, CampaignDispatchRecipient, ServiceQueue, User
from app.security import hash_password


def _login(client: TestClient, email: str, password: str) -> None:
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    assert token
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _campaign_and_queue(db: Session, suffix: str) -> tuple[Campaign, ServiceQueue]:
    campaign = Campaign(name=f"Campanha de exportação {suffix}")
    queue = ServiceQueue(name=f"Fila de exportação {suffix}", code=suffix)
    db.add_all([campaign, queue])
    db.flush()
    return campaign, queue


def test_dispatch_report_csv_has_all_recipients_and_their_send_results():
    failure_reason = "Número inválido; verifique a lista"
    blocked_reason = '  =texto; "bloqueado"\nárea restrita'
    sent_at = datetime(2026, 11, 20, 15, 0)
    with TestClient(app) as client:
        _login(client, settings.bootstrap_admin_email, settings.bootstrap_admin_password)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "CSV-REPORT")
            dispatch = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id, status="PAUSADO")
            other_dispatch = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id)
            db.add_all([dispatch, other_dispatch])
            db.flush()
            dispatch_id = dispatch.id
            results = [
                ("ENVIADO", sent_at, None),
                ("PENDENTE", None, None),
                ("FALHOU", None, failure_reason),
                ("IGNORADO", None, blocked_reason),
                ("ENVIANDO", None, None),
            ]
            results.extend([("PENDENTE", None, None)] * 496)
            db.add_all(
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id,
                    phone=f"555590080{number:04d}",
                    status=recipient_status,
                    sent_at=sent,
                    error=error,
                )
                for number, (recipient_status, sent, error) in enumerate(results, start=1)
            )
            other_phone = "5555900800999"
            db.add(CampaignDispatchRecipient(dispatch_id=other_dispatch.id, phone=other_phone))
            db.commit()

        report_path = f"/dispatches/{dispatch_id}/report.csv"
        page = client.get(f"/dispatches/{dispatch_id}", params={"status_filter": "FALHOU"})
        assert page.status_code == 200
        assert f'href="{report_path}">Baixar CSV</a>' in page.text
        response = client.get(report_path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-disposition"] == f'attachment; filename="disparo-{dispatch_id}-relatorio.csv"'
        assert response.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))

        assert len(rows) == 501
        # Colunas originais continuam estáveis para relatórios existentes.
        assert {key: rows[0][key] for key in ("Telefone", "Resultado", "Status", "Enviado em", "Motivo")} == {
            "Telefone": "5555900800001",
            "Resultado": "Enviado",
            "Status": "ENVIADO",
            "Enviado em": "20/11/2026 12:00",
            "Motivo": "",
        }
        assert rows[0]["Prova da entrega"] == "ACEITO SEM ID — NÃO CONFIRMADO"
        assert rows[0]["ID WhatsApp"] == ""
        assert rows[1]["Prova da entrega"] == "NÃO ENVIADO"
        assert [(row["Resultado"], row["Status"]) for row in rows[1:5]] == [
            ("Não enviado", "PENDENTE"),
            ("Não enviado", "FALHOU"),
            ("Não enviado", "IGNORADO"),
            ("Não enviado", "ENVIANDO"),
        ]
        assert rows[1]["Enviado em"] == ""
        assert rows[2]["Motivo"] == failure_reason
        assert rows[3]["Motivo"] == f"'{blocked_reason}"
        assert rows[-1]["Telefone"] == "5555900800501"
        assert all(row["Telefone"] != other_phone for row in rows)
        assert client.get("/dispatches/-1/report.csv").status_code == 404


def test_dispatch_report_download_requires_export_permission():
    email = "dispatch-report-view@sulnet.local"
    password = token_urlsafe(24)
    with TestClient(app) as client:
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "CSV-PERMISSION")
            dispatch = CampaignDispatch(campaign_id=campaign.id, queue_id=queue.id)
            db.add_all([
                dispatch,
                User(
                    name="Leitura de disparos",
                    email=email,
                    password_hash=hash_password(password),
                    role="CONSULTOR",
                    must_change_password=False,
                    access_permissions=json.dumps(["dispatches.view"]),
                ),
            ])
            db.flush()
            detail_path = f"/dispatches/{dispatch.id}"
            db.commit()

        report_path = f"{detail_path}/report.csv"
        response = client.get(report_path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        _login(client, email, password)
        page = client.get(detail_path)
        assert page.status_code == 200
        assert "Baixar CSV" not in page.text
        assert client.get(report_path).status_code == 403
