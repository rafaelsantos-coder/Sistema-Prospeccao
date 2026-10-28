from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Campaign, CampaignRecipient, Lead
from app.services.audit import write_audit
from app.services.conversation_engine import ensure_contact, ensure_open_conversation, register_message
from app.services.zapi import ZApiError, ZApiService
from app.timeutils import local_day_start_utc, utcnow


def campaign_message_template(campaign: Campaign) -> str:
    try:
        sequence = json.loads(campaign.sequence_json or "[]")
    except (TypeError, json.JSONDecodeError):
        sequence = []
    if isinstance(sequence, list):
        for step in sequence:
            if isinstance(step, dict) and step.get("message"):
                return str(step["message"]).strip()
    return ""


def personalize_message(template: str, lead: Lead) -> str:
    values = {
        "nome": lead.contact_name or lead.business_name,
        "empresa": lead.business_name,
        "cidade": lead.city,
        "segmento": lead.segment,
    }
    result = template
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", str(value or ""))
        result = result.replace("{{ " + key + " }}", str(value or ""))
    return result.strip()


def enqueue_campaign(db: Session, campaign: Campaign, *, max_leads: int = 1000) -> int:
    template = campaign_message_template(campaign)
    if not template:
        raise ValueError("A campanha precisa ter uma mensagem de abordagem.")

    stmt = select(Lead).where(
        Lead.phone.is_not(None),
        Lead.do_not_contact.is_(False),
        Lead.stage.not_in({"NAO_CONTATAR", "PERDIDO", "GANHO"}),
    )
    if campaign.branch_id:
        stmt = stmt.where(Lead.branch_id == campaign.branch_id)
    if campaign.team:
        stmt = stmt.where(Lead.team == campaign.team)
    if campaign.segment:
        stmt = stmt.where(Lead.segment.ilike(f"%{campaign.segment}%"))
    leads = list(db.scalars(stmt.order_by(Lead.score.desc(), Lead.created_at.desc()).limit(max_leads)))

    created = 0
    for lead in leads:
        exists = db.scalar(
            select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.lead_id == lead.id,
            )
        )
        if exists:
            continue
        body = personalize_message(template, lead)
        if not body:
            continue
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                lead_id=lead.id,
                status="PENDENTE",
                message_body=body,
                scheduled_at=utcnow(),
            )
        )
        created += 1
    if created:
        campaign.status = "ATIVA"
    return created


def campaign_queue_counts(db: Session, campaign_id: int) -> dict[str, int]:
    rows = db.execute(
        select(CampaignRecipient.status, func.count(CampaignRecipient.id))
        .where(CampaignRecipient.campaign_id == campaign_id)
        .group_by(CampaignRecipient.status)
    ).all()
    result = {str(status): int(count) for status, count in rows}
    return {
        "pending": result.get("PENDENTE", 0),
        "sending": result.get("ENVIANDO", 0),
        "sent": result.get("ENVIADO", 0),
        "failed": result.get("FALHOU", 0),
        "skipped": result.get("IGNORADO", 0),
        "total": sum(result.values()),
    }


def _sent_today(db: Session, campaign: Campaign) -> int:
    return int(
        db.scalar(
            select(func.count(CampaignRecipient.id)).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == "ENVIADO",
                CampaignRecipient.sent_at >= local_day_start_utc(),
            )
        )
        or 0
    )


def claim_next_recipient(db: Session) -> CampaignRecipient | None:
    now = utcnow()
    recipient = db.scalar(
        select(CampaignRecipient)
        .join(Campaign, CampaignRecipient.campaign_id == Campaign.id)
        .where(
            CampaignRecipient.status == "PENDENTE",
            Campaign.status == "ATIVA",
            (CampaignRecipient.scheduled_at.is_(None) | (CampaignRecipient.scheduled_at <= now)),
        )
        .order_by(CampaignRecipient.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if recipient:
        recipient.status = "ENVIANDO"
        recipient.attempts = int(recipient.attempts or 0) + 1
        db.flush()
    return recipient


def process_recipient(db: Session, recipient: CampaignRecipient) -> str:
    campaign = db.get(Campaign, recipient.campaign_id)
    lead = db.get(Lead, recipient.lead_id)
    if not campaign or not lead:
        recipient.status = "IGNORADO"
        recipient.error = "Campanha ou lead inexistente."
        return "skipped"
    if campaign.status != "ATIVA":
        recipient.status = "PENDENTE"
        return "paused"
    if _sent_today(db, campaign) >= int(campaign.daily_limit or 1):
        recipient.status = "PENDENTE"
        recipient.scheduled_at = utcnow() + timedelta(minutes=30)
        return "limit"
    if lead.do_not_contact or lead.stage in {"NAO_CONTATAR", "PERDIDO", "GANHO"} or not lead.phone:
        recipient.status = "IGNORADO"
        recipient.error = "Lead bloqueado, encerrado ou sem telefone."
        return "skipped"

    try:
        result = ZApiService().send_text(phone=lead.phone, message=recipient.message_body)
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        if recipient.attempts >= 3:
            recipient.status = "FALHOU"
        else:
            recipient.status = "PENDENTE"
            recipient.scheduled_at = utcnow() + timedelta(minutes=5 * recipient.attempts)
        return "failed"

    contact = ensure_contact(db, lead.phone, lead=lead)
    conversation = ensure_open_conversation(db, contact, lead=lead, source=f"CAMPANHA:{campaign.id}")
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=recipient.message_body,
        sender_user_id=campaign.created_by_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    now = utcnow()
    recipient.status = "ENVIADO"
    recipient.sent_at = now
    recipient.provider_message_id = provider_id
    recipient.conversation_id = conversation.id
    recipient.error = None
    lead.last_contact_at = now
    if lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
        lead.stage = "CONTATO_REALIZADO"
    db.add(
        Activity(
            lead_id=lead.id,
            user_id=campaign.created_by_id,
            kind="WHATSAPP",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=f"CAMPANHA_ZAPI:{provider_id or message.id}"[:120],
            description=f"Campanha {campaign.name}: {recipient.message_body}",
            completed_at=now,
        )
    )
    write_audit(
        db,
        user=None,
        action="CAMPAIGN_MESSAGE_SENT",
        entity_type="campaign_recipient",
        entity_id=recipient.id,
        payload={
            "campaign_id": campaign.id,
            "lead_id": lead.id,
            "conversation_id": conversation.id,
            "provider_message_id": provider_id,
        },
    )
    return "sent"
