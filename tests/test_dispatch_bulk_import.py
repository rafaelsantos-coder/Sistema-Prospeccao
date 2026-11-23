from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models import Lead, LeadQueuePreference, ServiceQueue
from app.services.dispatch_queue import prepare_dispatch_leads_bulk


def test_bulk_import_prepares_large_csv_without_duplicate_preferences():
    with TestClient(app):
        db = SessionLocal()
        try:
            queue = ServiceQueue(
                name="Fila CSV em lotes",
                code="CSV-IMPORT-BULK",
                team="DIGITAL",
                routing_strategy="ROUND_ROBIN",
                active=True,
            )
            db.add(queue)
            db.flush()
            existing = Lead(
                business_name="Lead preexistente",
                phone="5555999000000",
                city="Santa Rosa",
                stage="PERDIDO",
                do_not_contact=True,
            )
            db.add(existing)
            db.flush()
            original_id = existing.id

            rows = [
                {"phone": f"5555999{i:06d}", "name": f"Cliente {i}"}
                for i in range(240)
            ]
            leads = prepare_dispatch_leads_bulk(
                db, rows=rows, queue=queue, created_by_id=None,
            )
            assert len(leads) == 240
            assert leads[0].id == original_id
            assert leads[0].stage == "PERDIDO"
            assert leads[0].do_not_contact is True
            assert db.scalar(
                select(func.count(LeadQueuePreference.id))
                .where(LeadQueuePreference.queue_id == queue.id)
            ) == 240

            # Importar os mesmos contatos novamente só atualiza a preferência,
            # sem criar novas linhas, alterar bloqueios ou duplicar leads.
            repeated = prepare_dispatch_leads_bulk(
                db, rows=rows, queue=queue, created_by_id=None,
            )
            assert [lead.id for lead in repeated] == [lead.id for lead in leads]
            assert db.scalar(
                select(func.count(LeadQueuePreference.id))
                .where(LeadQueuePreference.queue_id == queue.id)
            ) == 240
            db.rollback()
        finally:
            db.close()
