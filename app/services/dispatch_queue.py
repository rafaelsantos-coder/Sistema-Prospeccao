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
from app.services.campaign_queue import campaign_message_template, personalize_message
from app.services.conversation_engine import (
    ensure_contact,
    ensure_open_conversation,
    register_message,
)
from app.services.queue_routing import set_lead_queue_preference
from app.services.scoring import normalize_phone
from app.services.zapi import ZApiError, ZApiService
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
    try:
        result = ZApiService().send_text(phone=lead.phone, message=body)
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        if recipient.attempts >= 3:
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
    )
    provider_id = str(
        result.get("messageId") or result.get("id") or ""
    ).strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None

    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=body,
        sender_user_id=dispatch.created_by_id,
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
            "interval_seconds": dispatch.interval_seconds,
        },
    )
    _finish_if_done(db, dispatch)
    return "sent"
