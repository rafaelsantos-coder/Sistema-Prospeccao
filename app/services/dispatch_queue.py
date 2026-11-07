from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Lead,
    ServiceQueue,
)
from app.services.audit import write_audit
from app.services.campaign_queue import (
    campaign_media_attachments,
    campaign_message_template,
    campaign_quick_replies,
    personalize_message,
    send_campaign_outreach,
)
from app.services.conversation_engine import (
    ensure_contact,
    ensure_open_conversation,
    register_message,
)
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.meta_cloud import MetaTemplateRequired
from app.services.meta_numbers import queue_meta_number
from app.services.queue_routing import set_lead_queue_preference
from app.services.scoring import normalize_phone
from app.services.zapi import ZApiError
from app.services.zapi_instances import queue_instance, service_for_queue
from app.timeutils import local_day_start_utc, utcnow


CSV_NAME_ALIASES = {"nome", "name", "cliente", "empresa", "contato"}
CSV_PHONE_ALIASES = {
    "telefone",
    "phone",
    "celular",
    "whatsapp",
    "numero",
    "número",
    "fone",
}


def _clean_header(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_")


def parse_dispatch_csv(payload: bytes, *, max_rows: int = 10000) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not payload:
        raise ValueError("O arquivo CSV está vazio.")

    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Não foi possível ler o CSV.")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("O CSV precisa ter cabeçalho com nome e telefone.")

    header_map: dict[str, str] = {}
    for original in reader.fieldnames:
        clean = _clean_header(original)
        if clean in CSV_NAME_ALIASES:
            header_map["name"] = original
        if clean in CSV_PHONE_ALIASES:
            header_map["phone"] = original

    if "phone" not in header_map:
        raise ValueError(
            "Não encontrei a coluna de telefone. Use telefone, celular, whatsapp ou phone."
        )

    valid: list[dict[str, str]] = []
    invalid = 0
    duplicates = 0
    seen: set[str] = set()

    for index, row in enumerate(reader, start=2):
        if len(valid) + invalid + duplicates >= max_rows:
            break
        raw_phone = str(row.get(header_map["phone"], "") or "").strip()
        phone = normalize_phone(raw_phone)
        name = (
            str(row.get(header_map.get("name", ""), "") or "").strip()
            if header_map.get("name")
            else ""
        )
        if not phone or len(phone) < 10:
            invalid += 1
            continue
        if phone in seen:
            duplicates += 1
            continue
        seen.add(phone)
        valid.append(
            {
                "name": name or phone,
                "phone": phone,
                "row": str(index),
            }
        )

    summary = {
        "valid": len(valid),
        "invalid": invalid,
        "duplicates": duplicates,
        "delimiter": getattr(dialect, "delimiter", ";"),
        "has_name_column": bool(header_map.get("name")),
        "truncated": len(valid) + invalid + duplicates >= max_rows,
    }
    if not valid:
        raise ValueError("Nenhum telefone válido foi encontrado no CSV.")
    return valid, summary


def ensure_dispatch_lead(
    db: Session,
    *,
    name: str,
    phone: str,
    queue: ServiceQueue,
    created_by_id: int | None,
) -> Lead:
    lead = db.scalar(
        select(Lead)
        .where(Lead.phone == phone)
        .order_by(Lead.updated_at.desc(), Lead.id.desc())
        .limit(1)
    )
    if lead:
        if name and (not lead.business_name or lead.business_name == lead.phone):
            lead.business_name = name
        if name and not lead.contact_name:
            lead.contact_name = name
        set_lead_queue_preference(
            db,
            lead.id,
            queue.id,
            source="CSV_DISPATCH",
        )
        return lead

    city = queue.branch.city if queue.branch else "Não informado"
    lead = Lead(
        business_name=name or phone,
        contact_name=name or "",
        phone=phone,
        segment="Importação CSV",
        city=city,
        source="IMPORTACAO",
        origin_channel="WHATSAPP",
        team=queue.team,
        stage="NOVO",
        score=0,
        temperature="FRIO",
        priority="NORMAL",
        branch_id=queue.branch_id,
        created_by_id=created_by_id,
        notes="Lead criada automaticamente a partir de CSV de disparo.",
        raw_data=json.dumps(
            {"import_source": "campaign_dispatch_csv"},
            ensure_ascii=False,
        ),
    )
    db.add(lead)
    db.flush()
    set_lead_queue_preference(
        db,
        lead.id,
        queue.id,
        source="CSV_DISPATCH",
    )
    return lead


def dispatch_counts(db: Session, dispatch_id: int) -> dict[str, int]:
    rows = db.execute(
        select(
            CampaignDispatchRecipient.status,
            func.count(CampaignDispatchRecipient.id),
        )
        .where(CampaignDispatchRecipient.dispatch_id == dispatch_id)
        .group_by(CampaignDispatchRecipient.status)
    ).all()
    values = {str(status): int(count) for status, count in rows}
    total = sum(values.values())
    return {
        "pending": values.get("PENDENTE", 0),
        "sending": values.get("ENVIANDO", 0),
        "sent": values.get("ENVIADO", 0),
        "failed": values.get("FALHOU", 0),
        "skipped": values.get("IGNORADO", 0),
        "total": total,
    }


def estimated_duration_seconds(total: int, interval_seconds: int) -> int:
    return max(0, int(total) - 1) * max(1, int(interval_seconds))


def create_dispatch_from_leads(
    db: Session,
    *,
    campaign: Campaign,
    queue: ServiceQueue,
    leads: list[Lead],
    created_by_id: int | None,
    interval_seconds: int = 30,
    daily_limit: int = 100,
    auto_start: bool = False,
    source: str = "PROSPECTING",
) -> tuple[CampaignDispatch | None, dict[str, Any]]:
    """Monta um disparo a partir de leads já importados (ex.: busca Apify)."""
    template = campaign_message_template(campaign)
    if not template:
        raise ValueError("A campanha selecionada não possui mensagem válida.")

    interval = max(5, min(int(interval_seconds or 30), 3600))
    limit = max(1, min(int(daily_limit or campaign.daily_limit or 100), 5000))

    dispatch = CampaignDispatch(
        campaign_id=campaign.id,
        queue_id=queue.id,
        status="RASCUNHO",
        interval_seconds=interval,
        daily_limit=limit,
        created_by_id=created_by_id,
    )
    db.add(dispatch)
    db.flush()

    created = 0
    skipped_no_phone = 0
    skipped_blocked = 0
    skipped_no_message = 0
    seen_phones: set[str] = set()

    for lead in leads:
        phone = normalize_phone(lead.phone)
        if not phone:
            skipped_no_phone += 1
            continue
        if phone in seen_phones:
            continue
        seen_phones.add(phone)
        if lead.do_not_contact or lead.stage in {"NAO_CONTATAR", "PERDIDO", "GANHO"}:
            skipped_blocked += 1
            continue
        message = personalize_message(template, lead)
        if not message:
            skipped_no_message += 1
            continue
        set_lead_queue_preference(
            db,
            lead.id,
            queue.id,
            source=source,
        )
        db.add(
            CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                lead_id=lead.id,
                name=(lead.business_name or lead.contact_name or "").strip()[:180],
                phone=phone,
                message_body=message,
                status="PENDENTE",
            )
        )
        created += 1

    summary: dict[str, Any] = {
        "source": source,
        "imported": created,
        "blocked": skipped_blocked,
        "no_phone": skipped_no_phone,
        "no_message": skipped_no_message,
        "estimated_duration_seconds": estimated_duration_seconds(created, interval),
        "interval_seconds": interval,
        "daily_limit": limit,
        "auto_start": bool(auto_start),
    }
    dispatch.import_summary_json = json.dumps(summary, ensure_ascii=False)

    if created == 0:
        db.delete(dispatch)
        db.flush()
        return None, summary

    if auto_start:
        dispatch.status = "ATIVO"
        dispatch.started_at = utcnow()
        dispatch.next_send_at = utcnow()

    write_audit(
        db,
        user=None,
        action=(
            "CAMPAIGN_DISPATCH_STARTED"
            if auto_start
            else "CAMPAIGN_DISPATCH_CREATED"
        ),
        entity_type="campaign_dispatch",
        entity_id=dispatch.id,
        payload={
            "campaign_id": campaign.id,
            "queue_id": queue.id,
            "interval_seconds": interval,
            "daily_limit": limit,
            "source": source,
            "summary": summary,
            "auto_start": bool(auto_start),
            "created_by_id": created_by_id,
        },
    )
    return dispatch, summary


def _sent_today(db: Session, dispatch_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(CampaignDispatchRecipient.id)).where(
                CampaignDispatchRecipient.dispatch_id == dispatch_id,
                CampaignDispatchRecipient.status == "ENVIADO",
                CampaignDispatchRecipient.sent_at >= local_day_start_utc(),
            )
        )
        or 0
    )


def claim_next_dispatch_recipient(
    db: Session,
) -> CampaignDispatchRecipient | None:
    now = utcnow()
    recipient = db.scalar(
        select(CampaignDispatchRecipient)
        .join(
            CampaignDispatch,
            CampaignDispatchRecipient.dispatch_id == CampaignDispatch.id,
        )
        .where(
            CampaignDispatchRecipient.status == "PENDENTE",
            CampaignDispatch.status == "ATIVO",
            (
                CampaignDispatch.next_send_at.is_(None)
                | (CampaignDispatch.next_send_at <= now)
            ),
        )
        .order_by(
            CampaignDispatch.next_send_at.asc().nullsfirst(),
            CampaignDispatch.created_at.asc(),
            CampaignDispatchRecipient.id.asc(),
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not recipient:
        return None

    dispatch = db.get(CampaignDispatch, recipient.dispatch_id)
    if not dispatch:
        return None

    recipient.status = "ENVIANDO"
    dispatch.next_send_at = now + timedelta(
        seconds=max(1, int(dispatch.interval_seconds or 1))
    )
    db.flush()
    return recipient


def _finish_if_done(db: Session, dispatch: CampaignDispatch) -> None:
    remaining = int(
        db.scalar(
            select(func.count(CampaignDispatchRecipient.id)).where(
                CampaignDispatchRecipient.dispatch_id == dispatch.id,
                CampaignDispatchRecipient.status.in_({"PENDENTE", "ENVIANDO"}),
            )
        )
        or 0
    )
    if remaining == 0:
        dispatch.status = "CONCLUIDO"
        dispatch.finished_at = utcnow()
        dispatch.next_send_at = None


def process_dispatch_recipient(
    db: Session,
    recipient: CampaignDispatchRecipient,
) -> str:
    dispatch = db.get(CampaignDispatch, recipient.dispatch_id)
    if not dispatch:
        recipient.status = "IGNORADO"
        recipient.error = "Disparo inexistente."
        return "skipped"

    campaign = db.get(Campaign, dispatch.campaign_id)
    queue = db.get(ServiceQueue, dispatch.queue_id)
    lead = db.get(Lead, recipient.lead_id) if recipient.lead_id else None

    if not campaign or not queue or not queue.active:
        recipient.status = "FALHOU"
        recipient.error = "Campanha ou fila de atendimento indisponível."
        _finish_if_done(db, dispatch)
        return "failed"

    if dispatch.status != "ATIVO":
        recipient.status = "PENDENTE"
        return "paused"

    if _sent_today(db, dispatch.id) >= max(1, int(dispatch.daily_limit or 1)):
        recipient.status = "PENDENTE"
        dispatch.next_send_at = local_day_start_utc() + timedelta(
            days=1,
            seconds=5,
        )
        return "limit"

    if not lead:
        lead = ensure_dispatch_lead(
            db,
            name=recipient.name,
            phone=recipient.phone,
            queue=queue,
            created_by_id=dispatch.created_by_id,
        )
        recipient.lead_id = lead.id

    if (
        lead.do_not_contact
        or lead.stage in {"NAO_CONTATAR", "PERDIDO", "GANHO"}
        or not lead.phone
    ):
        recipient.status = "IGNORADO"
        recipient.error = "Lead bloqueada, encerrada ou sem telefone."
        _finish_if_done(db, dispatch)
        return "skipped"

    body = recipient.message_body.strip()
    if not body:
        template = campaign_message_template(campaign)
        body = personalize_message(template, lead)
        recipient.message_body = body
    if not body:
        recipient.status = "FALHOU"
        recipient.error = "Campanha sem mensagem válida."
        _finish_if_done(db, dispatch)
        return "failed"

    recipient.attempts = int(recipient.attempts or 0) + 1
    quick_replies = campaign_quick_replies(campaign)
    zapi_instance = queue_instance(db, queue.id)
    meta_number = queue_meta_number(db, queue.id)
    zapi = service_for_queue(db, queue.id)
    try:
        outreach = send_campaign_outreach(
            db,
            service=zapi,
            queue_id=queue.id,
            campaign=campaign,
            lead=lead,
            body=body,
            quick_replies=quick_replies,
        )
        result = outreach.result
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        if recipient.attempts >= 3 or isinstance(exc, MetaTemplateRequired):
            recipient.status = "FALHOU"
            _finish_if_done(db, dispatch)
        else:
            recipient.status = "PENDENTE"
            dispatch.next_send_at = utcnow() + timedelta(
                seconds=max(
                    int(dispatch.interval_seconds or 1),
                    60 * recipient.attempts,
                )
            )
        return "failed"

    contact = ensure_contact(db, lead.phone, lead=lead)
    conversation = ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source=f"DISPARO:{dispatch.id}",
        queue_id=queue.id,
        queue_origin="DISPATCH",
        assign_if_unowned=False,
        zapi_instance_id=zapi_instance.id if zapi_instance and not meta_number else None,
        meta_number_id=meta_number.id if meta_number else None,
    )
    provider_id = str(
        result.get("messageId") or result.get("id") or ""
    ).strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None

    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=outreach.message_type,
        body=outreach.body,
        sender_user_id=dispatch.created_by_id,
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
                    sender_user_id=dispatch.created_by_id,
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
            user_id=dispatch.created_by_id,
            kind="WHATSAPP",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=f"DISPARO_ZAPI:{provider_id or message.id}"[:120],
            description=(
                f"Disparo #{dispatch.id} · Campanha {campaign.name}: {body}"
            ),
            completed_at=now,
        )
    )
    write_audit(
        db,
        user=None,
        action="DISPATCH_MESSAGE_SENT",
        entity_type="campaign_dispatch_recipient",
        entity_id=recipient.id,
        payload={
            "dispatch_id": dispatch.id,
            "campaign_id": campaign.id,
            "queue_id": queue.id,
            "lead_id": lead.id,
            "conversation_id": conversation.id,
            "provider_message_id": provider_id,
            "zapi_instance_id": zapi_instance.id if zapi_instance else None,
            "quick_reply_count": len(quick_replies),
            "media_sent": media_sent,
            "media_errors": media_errors,
            "interval_seconds": dispatch.interval_seconds,
        },
    )
    _finish_if_done(db, dispatch)
    return "sent"
