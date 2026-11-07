from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Campaign, CampaignRecipient, Lead
from app.services.audit import write_audit
from app.services.conversation_engine import ensure_contact, ensure_open_conversation, register_message
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.meta_cloud import MetaTemplateRequired
from app.services.meta_numbers import queue_meta_number
from app.services.queue_routing import queue_for_campaign
from app.services.zapi import ZApiError
from app.services.zapi_instances import queue_instance, service_for_queue
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


def campaign_meta_template(campaign: Campaign) -> dict | None:
    """Template da API oficial da Meta escolhido para a campanha (ou None)."""
    try:
        sequence = json.loads(campaign.sequence_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(sequence, list):
        return None
    for step in sequence:
        if not isinstance(step, dict) or not step.get("message"):
            continue
        config = step.get("meta_template")
        if not isinstance(config, dict) or not config.get("template_id"):
            return None
        params = config.get("params")
        return {
            "template_id": int(config["template_id"]),
            "params": [str(item) for item in params] if isinstance(params, list) else [],
        }
    return None


def set_campaign_meta_template(
    campaign: Campaign,
    *,
    template_id: int | None,
    params: list[str],
) -> None:
    try:
        sequence = json.loads(campaign.sequence_json or "[]")
    except (TypeError, json.JSONDecodeError):
        sequence = []
    if not isinstance(sequence, list):
        sequence = []
    for step in sequence:
        if isinstance(step, dict) and step.get("message"):
            if template_id:
                step["meta_template"] = {"template_id": int(template_id), "params": params}
            else:
                step.pop("meta_template", None)
            break
    else:
        raise ValueError("A campanha precisa ter uma mensagem de abordagem.")
    campaign.sequence_json = json.dumps(sequence, ensure_ascii=False)


def personalize_template_param(value: str, lead: Lead) -> str:
    """Variável de template: aceita "{{nome}}|cliente" (valor padrão se vazio)."""
    raw, _, default = value.partition("|")
    result = personalize_message(raw, lead)
    return result or default.strip()


class OutreachSent:
    """Resultado de um envio de campanha/disparo, igual para Z-API e Meta."""

    def __init__(self, *, result: dict, body: str, message_type: str, template_used: bool) -> None:
        self.result = result
        self.body = body
        self.message_type = message_type
        self.template_used = template_used


def send_campaign_outreach(
    db: Session,
    *,
    service,
    queue_id: int,
    campaign: Campaign,
    lead: Lead,
    body: str,
    quick_replies: list[dict[str, str]],
) -> OutreachSent:
    """Envia a abordagem da campanha pelo canal da fila.

    Z-API: exatamente o envio de antes. API oficial da Meta: com template
    configurado envia o template; sem template só envia texto livre se o lead
    falou com o número nas últimas 24h (regra da Meta).
    """
    from app.services.meta_cloud import MetaCloudService

    if not isinstance(service, MetaCloudService):
        if quick_replies:
            result = service.send_button_list(phone=lead.phone, message=body, buttons=quick_replies)
        else:
            result = service.send_text(phone=lead.phone, message=body)
        return OutreachSent(
            result=result,
            body=body,
            message_type="BUTTONS" if quick_replies else "TEXT",
            template_used=False,
        )

    from app.models import MetaMessageTemplate
    from app.services.meta_cloud import MetaCloudError, MetaTemplateRequired
    from app.services.meta_numbers import build_template_message, meta_window_open, queue_meta_number

    meta_number = queue_meta_number(db, queue_id)
    config = campaign_meta_template(campaign)
    if config:
        template = db.get(MetaMessageTemplate, config["template_id"])
        if not template or template.status != "APPROVED":
            raise MetaTemplateRequired(
                "O template da campanha não está mais aprovado na Meta. Escolha outro template."
            )
        if meta_number and template.meta_number and template.meta_number.waba_id != meta_number.waba_id:
            raise MetaCloudError(
                "O template da campanha pertence a outra conta WhatsApp Business que não a da fila."
            )
        values = [personalize_template_param(item, lead) for item in config["params"]]
        header_url = None
        header_format = (template.header_format or "").upper()
        if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
            wanted = {"IMAGE": "IMAGE", "VIDEO": "VIDEO", "DOCUMENT": "DOCUMENT"}[header_format]
            for attachment in campaign_media_attachments(campaign):
                if attachment.get("type") == wanted:
                    try:
                        header_url = MediaStorage().presigned_download_url(
                            str(attachment.get("storage_uri") or ""),
                            filename=str(attachment.get("file_name") or "arquivo"),
                            expires_seconds=3600,
                        )
                    except MediaStorageError as exc:
                        raise MetaCloudError(f"Mídia do cabeçalho indisponível: {exc}") from exc
                    break
        components, rendered = build_template_message(template, values, header_media_url=header_url)
        result = service.send_template(
            phone=lead.phone,
            name=template.name,
            language=template.language,
            components=components,
        )
        return OutreachSent(result=result, body=rendered, message_type="TEXT", template_used=True)

    if not meta_number or not meta_window_open(db, meta_number.id, lead.phone):
        raise MetaTemplateRequired(
            "Fila na API oficial da Meta: este lead não falou com o número nas últimas 24h. "
            "Escolha um template aprovado na campanha para poder enviar."
        )
    if quick_replies:
        result = service.send_button_list(phone=lead.phone, message=body, buttons=quick_replies)
    else:
        result = service.send_text(phone=lead.phone, message=body)
    return OutreachSent(
        result=result,
        body=body,
        message_type="BUTTONS" if quick_replies else "TEXT",
        template_used=False,
    )


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

    service_queue = queue_for_campaign(db, campaign.id)
    if not service_queue:
        recipient.status = "FALHOU"
        recipient.error = "Campanha sem fila de atendimento configurada."
        return "failed"

    recipient.attempts = int(recipient.attempts or 0) + 1
    quick_replies = campaign_quick_replies(campaign)
    zapi_instance = queue_instance(db, service_queue.id)
    meta_number = queue_meta_number(db, service_queue.id)
    zapi = service_for_queue(db, service_queue.id)
    try:
        outreach = send_campaign_outreach(
            db,
            service=zapi,
            queue_id=service_queue.id,
            campaign=campaign,
            lead=lead,
            body=recipient.message_body,
            quick_replies=quick_replies,
        )
        result = outreach.result
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        if recipient.attempts >= 3 or isinstance(exc, MetaTemplateRequired):
            recipient.status = "FALHOU"
        else:
            recipient.status = "PENDENTE"
            recipient.scheduled_at = utcnow() + timedelta(minutes=5 * recipient.attempts)
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
        zapi_instance_id=zapi_instance.id if zapi_instance and not meta_number else None,
        meta_number_id=meta_number.id if meta_number else None,
    )
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=outreach.message_type,
        body=outreach.body,
        sender_user_id=campaign.created_by_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )

    media_sent = 0
    media_errors: list[str] = []
    # Template da Meta já leva a mídia no cabeçalho; fora da janela de 24h a
    # Meta recusaria mídia avulsa.
    attachments = [] if outreach.template_used else campaign_media_attachments(campaign)
    if attachments:
        storage = MediaStorage()
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
            "zapi_instance_id": zapi_instance.id if zapi_instance else None,
            "quick_reply_count": len(quick_replies),
            "media_sent": media_sent,
            "media_errors": media_errors,
        },
    )
    return "sent"
