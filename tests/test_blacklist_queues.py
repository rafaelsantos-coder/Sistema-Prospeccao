"""Regressão: campanhas já enfileiradas devem ser ignoradas após bloqueio."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Campaign, CampaignDispatch, CampaignDispatchRecipient,
    CampaignRecipient, Lead, ServiceQueue,
)
from app.services.campaign_queue import process_recipient
from app.services.dispatch_queue import process_dispatch_recipient
from app.services.global_blacklist import block_phone


def test_preexisting_campaign_and_dispatch_queue_never_send_blacklisted_number(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            blocked_phone = "5555999336677"
            block_phone(db, phone=blocked_phone, source="CSV")
            # Lead novo/reimportado, ainda sem sinalizador local.
            lead = Lead(business_name="Novo lead", phone=blocked_phone, do_not_contact=False)
            queue = ServiceQueue(name="Fila digital teste", code="BLTEST", team="LOJA", active=True)
            campaign = Campaign(name="Campanha teste", status="ATIVA", daily_limit=20)
            db.add_all([lead, queue, campaign])
            db.flush()
            job = CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead.id, status="PENDENTE", message_body="Oferta",
            )
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                daily_limit=50,
            )
            db.add_all([job, dispatch])
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id,
                phone=blocked_phone, name="Novo lead", status="PENDENTE",
                message_body="Oferta disparo",
            )
            db.add(recipient)
            db.flush()

            def must_not_send(*args, **kwargs):
                raise AssertionError("Não pode acessar instância ou enviar para blacklist")
            monkeypatch.setattr(
                "app.services.campaign_queue.service_for_queue", must_not_send
            )
            monkeypatch.setattr(
                "app.services.dispatch_queue.service_for_queue", must_not_send
            )
            assert process_recipient(db, job) == "skipped"
            assert job.status == "IGNORADO"
            assert job.attempts == 0
            assert process_dispatch_recipient(db, recipient) == "skipped"
            assert recipient.status == "IGNORADO"
            assert recipient.attempts == 0
    finally:
        engine.dispose()
