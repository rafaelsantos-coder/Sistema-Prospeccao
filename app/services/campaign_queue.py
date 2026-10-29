from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Campaign, CampaignRecipient, Lead
from app.services.audit import write_audit
from app.services.conversation_engine import ensure_contact, ensure_open_conversation, register_message
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.queue_routing import queue_for_campaign
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


def campaign_media_attachments(campaign: Campaign) -> list[dict[str, str]]:
    try:
        sequence = json.loads(campaign.sequence_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(sequence, list):
        return []
    for step in sequence:
        if not isinstance(step, dict) or not step.get("message"):
            continue
        media = step.get("media")
        if not isinstance(media, list):
            return []
        return [
            item
            for item in media
            if isinstance(item, dict)
            and item.get("type") in {"IMAGE", "VIDEO", "AUDIO"}
            and item.get("storage_uri")
        ]
    return []


def campaign_quick_replies(campaign: Campaign) -> list[dict[str, str]]:
    try:
        sequence = json.loads(campaign.sequence_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(sequence, list):
        return []
    for step in sequence:
        if not isinstance(step, dict) or not step.get("message"):
            continue
        replies = step.get("quick_replies")
        if not isinstance(replies, list):
            return []
        return [
            {
                "id": str(item.get("id") or "").strip(),
                "label": str(item.get("label") or "").strip(),
            }
            for item in replies[:3]
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ]
    return []


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
        ~Lead.stage.in_({"NAO_CONTATAR", "PERDIDO", "GANHO"}),
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

    recipient.attempts = int(recipient.attempts or 0) + 1
    quick_replies = campaign_quick_replies(campaign)
    try:
        zapi = ZApiService()
        if quick_replies:
            result = zapi.send_button_list(
                phone=lead.phone,
                message=recipient.message_body,
                buttons=quick_replies,
            )
        else:
            result = zapi.send_text(phone=lead.phone, message=recipient.message_body)
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        if recipient.attempts >= 3:
            recipient.status = "FALHOU"
        else:
            recipient.status = "PENDENTE"
            recipient.scheduled_at = utcnow() + timedelta(minutes=5 * recipient.attempts)
        return "failed"

    service_queue = queue_for_campaign(db, campaign.id)
    if not service_queue:
        recipient.status = "FALHOU"
        recipient.error = "Campanha sem fila de atendimento configurada."
        return "failed"

    contact = ensure_contact(db, lead.phone, lead=lead)
    conversation = ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source=f"CAMPANHA:{campaign.id}",
        queue_id=service_queue.id,
        queue_origin="CAMPAIGN",
        assign_if_unowned=False,
    )
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="BUTTONS" if quick_replies else "TEXT",
        body=recipient.message_body,
        sender_user_id=campaign.created_by_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )

    media_sent = 0
    media_errors: list[str] = []
    attachments = campaign_media_attachments(campaign)
    if attachments:
        storage = MediaStorage()
        zapi = ZApiService()
        for attachment in attachments:
            kind = str(attachment.get("type") or "")
            storage_uri = str(attachment.get("storage_uri") or "")
            file_name = str(attachment.get("file_name") or "arquivo")
            mime_type = str(attachment.get("mime_type") or "application/octet-stream")
            try:
                delivery_url = storage.presigned_download_url(
                    storage_uri,
                    filename=file_name,
                    expires_seconds=3600,
                )
                if kind == "IMAGE":
                    media_result = zapi.send_image(
                        phone=lead.phone,
                        image_url=delivery_url,
                    )
                    media_body = "[Imagem]"
                elif kind == "VIDEO":
                    media_result = zapi.send_video(
                        phone=lead.phone,
                        video_url=delivery_url,
                    )
                    media_body = "[Video]"
                else:
                    media_result = zapi.send_audio(
                        phone=lead.phone,
                        audio_url=delivery_url,
                    )
                    media_body = "[Audio]"
                media_provider_id = str(
                    media_result.get("messageId") or media_result.get("id") or ""
                ).strip() or None
                media_zaap_id = str(media_result.get("zaapId") or "").strip() or None
                register_message(
                    db,
                    conversation,
                    direction="OUTBOUND",
                    message_type=kind,
                    body=media_body,
                    sender_user_id=campaign.created_by_id,
                    media_url=storage_uri,
                    mime_type=mime_type,
                    provider_message_id=media_provider_id,
                    provider_zaap_id=media_zaap_id,
                    status="SENT",
                )
                media_sent += 1
            except (MediaStorageError, ZApiError) as exc:
                media_errors.append(f"{file_name}: {exc}")

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
            "queue_id": service_queue.id,
            "quick_reply_count": len(quick_replies),
            "media_sent": media_sent,
            "media_errors": media_errors,
        },
    )
    return "sent"
