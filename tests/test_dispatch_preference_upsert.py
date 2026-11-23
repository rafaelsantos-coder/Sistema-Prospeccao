from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import LeadQueuePreference, ServiceQueue
from app.services.queue_routing import set_lead_queue_preference
from app.services.dispatch_queue import ensure_dispatch_lead


def test_dispatch_preference_upsert_keeps_one_row_after_repeated_imports():
    db = SessionLocal()
    try:
        queue_a = ServiceQueue(
            name="Fila CSV Upsert A",
            code="CSV-UPSERT-A",
            team="DIGITAL",
            routing_strategy="ROUND_ROBIN",
            active=True,
        )
        queue_b = ServiceQueue(
            name="Fila CSV Upsert B",
            code="CSV-UPSERT-B",
            team="DIGITAL",
            routing_strategy="ROUND_ROBIN",
            active=True,
        )
        db.add_all([queue_a, queue_b])
        db.flush()
        lead = ensure_dispatch_lead(
            db,
            name="Contato CSV Upsert",
            phone="5555999887766",
            queue=queue_a,
            created_by_id=None,
        )
        first = db.scalar(
            select(LeadQueuePreference).where(LeadQueuePreference.lead_id == lead.id)
        )
        assert first is not None
        first_id = first.id

        updated = set_lead_queue_preference(
            db, lead.id, queue_b.id, source="CSV_DISPATCH"
        )
        db.flush()
        assert updated.id == first_id
        assert updated.queue_id == queue_b.id
        assert first.queue_id == queue_b.id
        assert db.scalars(
            select(LeadQueuePreference).where(LeadQueuePreference.lead_id == lead.id)
        ).all() == [updated]
        db.rollback()
    finally:
        db.close()
