"""Conferência gerencial de resultados incertos, sem reenvio nem escrita."""
from __future__ import annotations

import re
import csv
import io
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignQueueLink,
    CampaignDispatch,
    CampaignDispatchRecipient,
    CampaignRecipient,
    Lead,
    ServiceQueue,
    User,
)
from app.security import hash_password
from app.timeutils import utcnow


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str = "admin@sulnet.local",
           password: str = "Sulnet@2026!") -> None:
    token = _csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={"csrf_token": token, "email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_reconciliation_lists_only_uncertain_and_stale_without_sending(monkeypatch):
    from app.services.zapi import ZApiService

    def forbid_send(*_args, **_kwargs):
        raise AssertionError("A conferência nunca pode chamar o provedor.")

    monkeypatch.setattr(ZApiService, "send_text", forbid_send)
    unique = uuid4().hex[:9]
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            campaign = Campaign(
                name=f"Conferência interna {unique}",
                channel="WHATSAPP", status="PAUSADA",
                daily_limit=10, sequence_json="[]",
            )
            queue = ServiceQueue(
                name=f"Fila de conferência {unique}",
                code=f"RECON-{unique}", team="DIGITAL",
                active=True, routing_strategy="ROUND_ROBIN",
            )
            db.add_all([campaign, queue])
            db.flush()
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="PAUSADO", interval_seconds=30, daily_limit=10,
            )
            db.add(dispatch)
            db.flush()
            created = utcnow()
            records = [
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, phone="5555999998501",
                    name=f"Empresa {unique} <script>alert(1)</script>",
                    status="INDETERMINADO", attempts=1,
                    error="provedor nao respondeu",
                    updated_at=created,
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, phone="5555999998502",
                    name=f"Contato antigo {unique}",
                    status="ENVIANDO", attempts=1,
                    updated_at=created - timedelta(minutes=35),
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, phone="5555999998503",
                    name=f"Contato recente {unique}",
                    status="ENVIANDO", attempts=1,
                    updated_at=created,
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, phone="5555999998504",
                    name=f"Contato pendente {unique}",
                    status="PENDENTE", attempts=0,
                ),
            ]
            db.add_all(records)
            lead = Lead(
                business_name=f"Lead legado {unique}",
                phone="5555999998505", stage="NOVO", team="DIGITAL",
            )
            db.add(lead)
            db.flush()
            legacy = CampaignRecipient(
                campaign_id=campaign.id,
                lead_id=lead.id,
                status="INDETERMINADO", attempts=1,
            )
            db.add(legacy)
            db.add(CampaignQueueLink(campaign_id=campaign.id, queue_id=queue.id))
            db.commit()
            queue_id = queue.id
            queue_name = queue.name
            record_ids = [item.id for item in records]
            legacy_id = legacy.id

        response = client.get("/dispatches/reconciliation", follow_redirects=False)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        page = response.text
        assert f"Empresa {unique} &lt;script&gt;" in page
        assert "<script>alert(1)</script>" not in page
        assert f"Contato antigo {unique}" in page
        assert f"Lead legado {unique}" in page
        assert f"Contato recente {unique}" not in page
        assert f"Contato pendente {unique}" not in page
        assert "Sem ID confirmado" in page
        by_queue = client.get(
            f"/dispatches/reconciliation?status=TODOS&queue_id={queue_id}"
        )
        assert by_queue.status_code == 200
        assert f"Empresa {unique}" in by_queue.text
        assert f"Lead legado {unique}" in by_queue.text
        assert f'value="{queue_id}" selected' in by_queue.text
        assert f"Empresa {unique}" not in client.get(
            "/dispatches/reconciliation?queue_id=99999999"
        ).text
        assert f"Lead legado {unique}" not in client.get(
            "/dispatches/reconciliation?queue_id=99999999"
        ).text
        assert "não reenviar automaticamente" in page

        export = client.get(
            f"/dispatches/reconciliation/export.csv?queue_id={queue_id}"
        )
        assert export.status_code == 200
        assert export.headers["cache-control"] == "private, no-store"
        assert export.headers["x-content-type-options"] == "nosniff"
        assert "attachment;" in export.headers["content-disposition"]
        assert export.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig")), delimiter=";"))
        assert any(r["Origem"] == "CSV" and f"Empresa {unique}" in r["Contato"] for r in rows)
        assert any(r["Origem"] == "FUNIL" and f"Lead legado {unique}" in r["Contato"] for r in rows)
        assert all(f"Contato recente {unique}" not in r["Contato"] for r in rows)
        assert all(f"Contato pendente {unique}" not in r["Contato"] for r in rows)
        assert all(r["Fila"] == queue_name for r in rows if unique in r["Campanha"])
        filtered_export = client.get(
            f"/dispatches/reconciliation/export.csv?queue_id={queue_id}&status=ENVIANDO"
        )
        filtered_rows = list(csv.DictReader(io.StringIO(filtered_export.text.lstrip("\ufeff")), delimiter=";"))
        assert any(f"Contato antigo {unique}" in r["Contato"] for r in filtered_rows)
        assert all(r["Situação"] == "ENVIANDO" for r in filtered_rows)

        only_uncertain = client.get(
            "/dispatches/reconciliation?status=INDETERMINADO"
        ).text
        assert f"Empresa {unique}" in only_uncertain
        assert f"Contato antigo {unique}" not in only_uncertain

        only_stale = client.get(
            "/dispatches/reconciliation?status=ENVIANDO"
        ).text
        assert f"Contato antigo {unique}" in only_stale
        assert f"Empresa {unique}" not in only_stale

        with SessionLocal() as db:
            assert [db.get(CampaignDispatchRecipient, i).status for i in record_ids] == [
                "INDETERMINADO", "ENVIANDO", "ENVIANDO", "PENDENTE",
            ]
            assert db.get(CampaignRecipient, legacy_id).status == "INDETERMINADO"


def test_reconciliation_auth_validation_and_no_store():
    with TestClient(app) as client:
        guest = client.get("/dispatches/reconciliation", follow_redirects=False)
        assert guest.status_code == 303
        assert guest.headers["location"] == "/login"
        _login(client)
        assert client.get("/dispatches/reconciliation?status=NOPE").status_code == 400
        assert client.get("/dispatches/reconciliation?page=0").status_code == 400
        assert client.get("/dispatches/reconciliation?page=201").status_code == 400
        assert client.get("/dispatches/reconciliation?queue_id=-1").status_code == 400
        assert client.get("/dispatches/reconciliation?status=TODOS").status_code == 200
        assert client.get("/dispatches/reconciliation/export.csv?status=NOPE").status_code == 400
        assert client.get("/dispatches/reconciliation/export.csv?queue_id=-1").status_code == 400


def test_reconciliation_is_forbidden_to_consultors():
    unique = uuid4().hex[:8]
    email = f"reconciliation-{unique}@sulnet.local"
    password = "ConsultorTest@2026!"
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(User(
                name=f"Consultor reconciliação {unique}",
                email=email, password_hash=hash_password(password),
                role="CONSULTOR", team="DIGITAL", active=True,
                must_change_password=False,
            ))
            db.commit()
        _login(client, email=email, password=password)
        denied = client.get("/dispatches/reconciliation", follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"] == "/dashboard"
        denied_csv = client.get("/dispatches/reconciliation/export.csv", follow_redirects=False)
        assert denied_csv.status_code == 303
        assert denied_csv.headers["location"] == "/dashboard"


def test_csv_formula_injection_is_escaped():
    from app.services.delivery_reconciliation import _safe_csv_cell

    assert _safe_csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _safe_csv_cell(" +SUM(1,2)") == "' +SUM(1,2)"
    assert _safe_csv_cell("@SUM(1,2)") == "'@SUM(1,2)"
    assert _safe_csv_cell("-1+2") == "'-1+2"
    assert _safe_csv_cell("\t=CMD()") == "'\t=CMD()"
    assert _safe_csv_cell("normal") == "normal"
