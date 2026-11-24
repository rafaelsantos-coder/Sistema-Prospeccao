from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import ColumnElement

from app.models import (
    Activity,
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Contact,
    Lead,
    Message,
    MessageEvent,
    ServiceQueue,
)
from app.services.audit import write_audit
from app.services.campaign_queue import (
    campaign_media_attachments,
    campaign_message_template,
    campaign_quick_replies,
    personalize_message,
)
from app.services.conversation_engine import (
    apply_provider_message_status,
    ensure_contact,
    ensure_open_conversation,
    register_message,
)
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.queue_routing import set_lead_queue_preference, set_lead_queue_preferences_bulk
from app.services.scoring import normalize_phone
from app.services.telegram import TelegramService, active_link_for_lead, invite_url_for_lead
from app.services.zapi import ZApiError, ZApiBlacklistedRecipient
from app.services.global_blacklist import is_globally_blocked
from app.services.zapi_instances import queue_instance, service_for_queue
from app.timeutils import LOCAL_TZ, local_day_start_utc, utcnow


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



def prepare_dispatch_leads_bulk(
    db: Session,
    *,
    rows: list[dict[str, str]],
    queue: ServiceQueue,
    created_by_id: int | None,
) -> list[Lead]:
    """Vincula contatos do CSV com poucas consultas, sem alterar o cadastro existente.

    Busca o lead mais recente por telefone, como ensure_dispatch_lead, e preserva
    nomes, bloqueios e demais campos. Não inicia o envio da campanha.
    """
    existing: dict[str, Lead] = {}
    phones = [row["phone"] for row in rows]
    for start in range(0, len(phones), 250):
        batch = phones[start : start + 250]
        matches = db.scalars(
            select(Lead)
            .where(Lead.phone.in_(batch))
            .order_by(Lead.updated_at.desc(), Lead.id.desc())
        )
        for lead in matches:
            if lead.phone:
                existing.setdefault(lead.phone, lead)

    city = queue.branch.city if queue.branch else "Não informado"
    leads: list[Lead] = []
    for row in rows:
        phone = row["phone"]
        name = row["name"]
        lead = existing.get(phone)
        if lead is None:
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
                raw_data=json.dumps({"import_source": "campaign_dispatch_csv"}, ensure_ascii=False),
            )
            db.add(lead)
            existing[phone] = lead
        else:
            if name and (not lead.business_name or lead.business_name == lead.phone):
                lead.business_name = name
            if name and not lead.contact_name:
                lead.contact_name = name
        leads.append(lead)

    db.flush()
    set_lead_queue_preferences_bulk(
        db, [lead.id for lead in leads], queue.id, source="CSV_DISPATCH"
    )
    return leads


def dispatch_counts_many(db: Session, dispatch_ids: list[int]) -> dict[int, dict[str, int]]:
    """Contadores por disparo, incluindo clientes únicos que responderam após o envio.

    Respostas são lidas do histórico real da conversa (qualquer tipo de mensagem
    recebida), sem depender de fila, IA, webhook extra ou nova migração.
    """
    ids = list(dict.fromkeys(dispatch_ids))
    if not ids:
        return {}

    rows = db.execute(
        select(
            CampaignDispatchRecipient.dispatch_id,
            CampaignDispatchRecipient.status,
            func.count(CampaignDispatchRecipient.id),
        )
        .where(CampaignDispatchRecipient.dispatch_id.in_(ids))
        .group_by(CampaignDispatchRecipient.dispatch_id, CampaignDispatchRecipient.status)
    ).all()
    per_dispatch: dict[int, dict[str, int]] = {dispatch_id: {} for dispatch_id in ids}
    for dispatch_id, status, count in rows:
        per_dispatch[int(dispatch_id)][str(status)] = int(count)

    # Uma resposta pode chegar em outra conversa do mesmo contato: reabertura,
    # transferência de fila ou mudança de instância Z-API alteram conversation_id.
    # A identidade do destinatário é o telefone (e a forma 12/13 dígitos BR).
    # Em disparos sucessivos para o mesmo número, atribuímos cada resposta ao
    # disparo mais recente anterior a ela, nunca a duas campanhas ao mesmo tempo.
    later = aliased(CampaignDispatchRecipient)
    recipient_phone_match = _sql_phones_equivalent(
        Contact.phone, CampaignDispatchRecipient.phone
    )
    later_phone_match = _sql_phones_equivalent(
        later.phone, CampaignDispatchRecipient.phone
    )
    superseded_before_reply = (
        select(later.id)
        .where(
            later_phone_match,
            later.status == "ENVIADO",
            later.sent_at.is_not(None),
            later.sent_at <= Message.created_at,
            or_(
                later.sent_at > CampaignDispatchRecipient.sent_at,
                and_(
                    later.sent_at == CampaignDispatchRecipient.sent_at,
                    later.id > CampaignDispatchRecipient.id,
                ),
            ),
        )
        .correlate(CampaignDispatchRecipient, Message)
        .exists()
    )
    inbound_after_send = (
        select(Message.id)
        .join(Contact, Contact.id == Message.contact_id)
        .where(
            recipient_phone_match,
            Message.direction == "INBOUND",
            Message.created_at > CampaignDispatchRecipient.sent_at,
            ~superseded_before_reply,
        )
        .correlate(CampaignDispatchRecipient)
        .exists()
    )
    response_rows = db.execute(
        select(
            CampaignDispatchRecipient.dispatch_id,
            func.count(CampaignDispatchRecipient.id),
        )
        .where(
            CampaignDispatchRecipient.dispatch_id.in_(ids),
            CampaignDispatchRecipient.status == "ENVIADO",
            CampaignDispatchRecipient.sent_at.is_not(None),
            inbound_after_send,
        )
        .group_by(CampaignDispatchRecipient.dispatch_id)
    ).all()
    responses = {int(dispatch_id): int(count) for dispatch_id, count in response_rows}

    # HTTP 200 no send-* significa solicitação aceita, NÃO entrega ao cliente.
    # A confirmação real depende dos callbacks de status (RECEIVED/READ/PLAYED).
    # Casa messageId e zaapId: o webhook pode referenciar qualquer um dos dois.
    # EXISTS evita double-count se mais de uma Message casar o mesmo destinatário.
    provider_message_match = or_(
        Message.provider_message_id == CampaignDispatchRecipient.provider_message_id,
        and_(
            CampaignDispatchRecipient.provider_message_id.is_not(None),
            Message.provider_zaap_id == CampaignDispatchRecipient.provider_message_id,
        ),
    )
    delivered_exists = (
        select(Message.id)
        .where(
            provider_message_match,
            or_(
                Message.delivered_at.is_not(None),
                Message.status.in_({"DELIVERED", "RECEIVED", "READ", "PLAYED"}),
            ),
        )
        .correlate(CampaignDispatchRecipient)
        .exists()
    )
    read_exists = (
        select(Message.id)
        .where(
            provider_message_match,
            or_(
                Message.read_at.is_not(None),
                Message.status.in_({"READ", "PLAYED"}),
            ),
        )
        .correlate(CampaignDispatchRecipient)
        .exists()
    )
    provider_failed_exists = (
        select(Message.id)
        .where(
            provider_message_match,
            Message.status.in_({"FAILED", "ERROR", "NOT_SENT", "UNDELIVERED"}),
        )
        .correlate(CampaignDispatchRecipient)
        .exists()
    )
    delivery_rows = db.execute(
        select(
            CampaignDispatchRecipient.dispatch_id,
            func.count(CampaignDispatchRecipient.id).filter(delivered_exists),
            func.count(CampaignDispatchRecipient.id).filter(read_exists),
            func.count(CampaignDispatchRecipient.id).filter(provider_failed_exists),
        )
        .where(
            CampaignDispatchRecipient.dispatch_id.in_(ids),
            CampaignDispatchRecipient.status == "ENVIADO",
        )
        .group_by(CampaignDispatchRecipient.dispatch_id)
    ).all()
    delivery = {
        int(dispatch_id): (int(delivered), int(read), int(provider_failed))
        for dispatch_id, delivered, read, provider_failed in delivery_rows
    }

    results: dict[int, dict[str, int]] = {}
    for dispatch_id in ids:
        values = per_dispatch[dispatch_id]
        sent = values.get("ENVIADO", 0)
        delivered, read, provider_failed = delivery.get(dispatch_id, (0, 0, 0))
        results[dispatch_id] = {
            "pending": values.get("PENDENTE", 0),
            "sending": values.get("ENVIANDO", 0),
            "sent": sent,
            "failed": values.get("FALHOU", 0),
            "skipped": values.get("IGNORADO", 0),
            "total": sum(values.values()),
            "replied": responses.get(dispatch_id, 0),
            "delivered": delivered,
            "read": read,
            "provider_failed": provider_failed,
            "unconfirmed": max(0, sent - delivered - provider_failed),
        }
    return results



def dispatch_delivery_proof(
    recipient_status: str,
    provider_message_id: str | None,
    provider_status: str | None = None,
    *,
    delivered_at: datetime | None = None,
    read_at: datetime | None = None,
) -> str:
    """Classifica apenas a entrega comprovada pelo provedor, nunca somente HTTP 200.

    Mensagens sem comprovante continuam não confirmadas, mesmo que o disparo
    original esteja marcado como ENVIADO. Não altera envios nem filas.
    """
    if recipient_status != "ENVIADO":
        return "NÃO ENVIADO"
    if not provider_message_id:
        return "ACEITO SEM ID — NÃO CONFIRMADO"
    current = (provider_status or "").upper().strip()
    if read_at or current in {"READ", "PLAYED"}:
        return "LIDO"
    if delivered_at or current in {"DELIVERED", "RECEIVED"}:
        return "ENTREGUE"
    if current in {"FAILED", "ERROR", "NOT_SENT", "UNDELIVERED"}:
        return "FALHA DE ENTREGA"
    return "ACEITO — SEM CONFIRMAÇÃO"


def dispatch_counts(db: Session, dispatch_id: int) -> dict[str, int]:
    return dispatch_counts_many(db, [dispatch_id])[dispatch_id]


def recipient_reply_flags(
    db: Session,
    recipients: list[CampaignDispatchRecipient],
) -> dict[int, bool]:
    """Indica, por destinatário da página, se o telefone respondeu após o envio."""
    from app.services.scoring import phone_match_keys

    flags = {int(item.id): False for item in recipients}
    enviados = [
        item
        for item in recipients
        if item.status == "ENVIADO" and item.sent_at and item.phone
    ]
    if not enviados:
        return flags

    phone_keys: set[str] = set()
    for item in enviados:
        phone_keys |= phone_match_keys(item.phone)
    if not phone_keys:
        return flags

    contacts = list(db.scalars(select(Contact).where(Contact.phone.in_(phone_keys))))
    contact_ids_by_key: dict[str, set[int]] = {}
    for contact in contacts:
        for key in phone_match_keys(contact.phone):
            contact_ids_by_key.setdefault(key, set()).add(int(contact.id))
    contact_ids = {int(contact.id) for contact in contacts}
    if not contact_ids:
        return flags

    inbound_rows = db.execute(
        select(Message.contact_id, Message.created_at).where(
            Message.contact_id.in_(contact_ids),
            Message.direction == "INBOUND",
        )
    ).all()
    for item in enviados:
        keys = phone_match_keys(item.phone)
        matching_ids = set()
        for key in keys:
            matching_ids |= contact_ids_by_key.get(key, set())
        if any(
            int(contact_id) in matching_ids and created_at > item.sent_at
            for contact_id, created_at in inbound_rows
        ):
            flags[int(item.id)] = True
    return flags


def _sql_brazilian_mobile_alternate(phone_col: ColumnElement[Any]) -> ColumnElement[Any]:
    """Expressão SQL da forma alternativa 12↔13 dígitos (9º dígito após DDD)."""
    return case(
        (
            and_(func.length(phone_col) == 13, func.substr(phone_col, 5, 1) == "9"),
            func.concat(func.substr(phone_col, 1, 4), func.substr(phone_col, 6)),
        ),
        (
            func.length(phone_col) == 12,
            func.concat(
                func.substr(phone_col, 1, 4),
                literal("9"),
                func.substr(phone_col, 5),
            ),
        ),
        else_=phone_col,
    )


def _sql_phones_equivalent(
    left: ColumnElement[Any], right: ColumnElement[Any]
) -> ColumnElement[Any]:
    """True se os telefones são iguais ou equivalentes no 9º dígito BR."""
    return or_(
        left == right,
        left == _sql_brazilian_mobile_alternate(right),
        _sql_brazilian_mobile_alternate(left) == right,
    )


def _reconcile_dispatch_message_status(db: Session, message: Message) -> None:
    """Aplica STATUS_UNMATCHED que chegaram no webhook antes do registro do disparo."""
    ids = [
        value
        for value in (message.provider_message_id, message.provider_zaap_id)
        if value
    ]
    if not ids:
        return
    if message.id is None:
        db.flush()
    for event in db.scalars(
        select(MessageEvent)
        .where(
            MessageEvent.provider_message_id.in_(ids),
            MessageEvent.event_type == "STATUS_UNMATCHED",
        )
        .order_by(MessageEvent.id)
    ):
        apply_provider_message_status(message, event.status or "", event.created_at)
        event.message_id = message.id
        event.event_type = "STATUS"


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_window(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """Valida a janela de envio. Ambos vazios = sem restrição. Levanta ValueError."""
    start = (start or "").strip()
    end = (end or "").strip()
    if not start and not end:
        return None, None
    if not start or not end:
        raise ValueError("Informe o horário de início e o de fim, ou deixe os dois em branco.")
    if not _HHMM.match(start) or not _HHMM.match(end):
        raise ValueError("Horário inválido. Use o formato HH:MM (ex.: 09:00).")
    if start >= end:
        raise ValueError("O horário de fim deve ser depois do início (a janela não pode virar a noite).")
    return start, end


def _as_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def window_next_start(dispatch: CampaignDispatch, now: datetime | None = None) -> datetime | None:
    """None se agora está dentro da janela (ou sem janela). Senão, quando ela abre (UTC sem fuso)."""
    if not dispatch.window_start or not dispatch.window_end:
        return None
    now_utc = now or utcnow()
    local = now_utc.replace(tzinfo=UTC).astimezone(LOCAL_TZ)
    start, end = _as_time(dispatch.window_start), _as_time(dispatch.window_end)
    if start <= local.time() < end:
        return None
    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    opens = datetime.combine(day, start, tzinfo=LOCAL_TZ)
    return opens.astimezone(UTC).replace(tzinfo=None)


def format_window(dispatch: CampaignDispatch) -> str:
    if dispatch.window_start and dispatch.window_end:
        return f"{dispatch.window_start} às {dispatch.window_end}"
    return "Sem restrição de horário"


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
    window_start: str | None = None,
    window_end: str | None = None,
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
        window_start=window_start,
        window_end=window_end,
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
        if lead.do_not_contact or lead.stage in {"NAO_CONTATAR", "PERDIDO", "GANHO"} or is_globally_blocked(db, phone):
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
    for _ in range(10):
        recipient = _claim_once(db)
        if recipient != "OUT_OF_WINDOW":
            return recipient
    return None


def _claim_once(db: Session) -> CampaignDispatchRecipient | None | str:
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

    opens_at = window_next_start(dispatch, now)
    if opens_at is not None:
        # Fora da janela de envio: o disparo espera abrir, sem consumir o contato.
        dispatch.next_send_at = opens_at
        db.flush()
        return "OUT_OF_WINDOW"

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
        or is_globally_blocked(db, recipient.phone)
        or is_globally_blocked(db, lead.phone)
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

    telegram = (dispatch.channel or "WHATSAPP") == "TELEGRAM"
    telegram_link = None
    if telegram:
        # Telegram só fala com quem já abriu o bot pelo link; nunca há envio a frio.
        telegram_link = active_link_for_lead(db, lead.id)
        if not telegram_link or not telegram_link.chat_id:
            recipient.status = "IGNORADO"
            recipient.error = "Cliente ainda não iniciou o Telegram."
            _finish_if_done(db, dispatch)
            return "skipped"
    elif "{{link_telegram}}" in body:
        # Convite: campanha de WhatsApp leva o link único do bot para o cliente aceitar.
        invite = invite_url_for_lead(db, lead)
        if not invite:
            recipient.status = "IGNORADO"
            recipient.error = "Telegram indisponível para este cliente."
            _finish_if_done(db, dispatch)
            return "skipped"
        body = body.replace("{{link_telegram}}", invite)
        recipient.message_body = body

    recipient.attempts = int(recipient.attempts or 0) + 1
    quick_replies = campaign_quick_replies(campaign)
    zapi_instance = None if telegram else queue_instance(db, queue.id)
    zapi = TelegramService(telegram_link.chat_id) if telegram_link else service_for_queue(db, queue.id)
    try:
        if quick_replies:
            result = zapi.send_button_list(
                phone=lead.phone,
                message=body,
                buttons=quick_replies,
            )
        else:
            result = zapi.send_text(
                phone=lead.phone,
                message=body,
            )
    except ZApiBlacklistedRecipient as exc:
        recipient.status = "IGNORADO"
        recipient.error = str(exc)[:500]
        _finish_if_done(db, dispatch)
        return "skipped"
    except ZApiError as exc:
        recipient.error = str(exc)[:500]
        http_status = getattr(exc, "status_code", None)
        if not telegram and http_status in {401, 403}:
            # Autorizacao rejeitada pela Z-API: nao gastar tres tentativas
            # do contato enquanto as credenciais/permissoes estao incorretas.
            # Pausar SOMENTE o disparo afetado; outras filas seguem operando.
            recipient.status = "PENDENTE"
            recipient.attempts = max(0, int(recipient.attempts or 0) - 1)
            recipient.error = (
                f"Z-API recusou envio (HTTP {http_status}). "
                "Revise conexao, token de seguranca e permissoes da instancia. "
                "Disparo pausado para preservar os contatos."
            )
            dispatch.status = "PAUSADO"
            dispatch.paused_at = utcnow()
            dispatch.next_send_at = None
            write_audit(
                db, user=None, action="DISPATCH_ZAPI_ACCESS_PAUSED",
                entity_type="campaign_dispatch", entity_id=dispatch.id,
                payload={"http_status": http_status, "recipient_id": recipient.id,
                         "queue_id": dispatch.queue_id, "attempt_preserved": True},
            )
            return "provider_access_paused"
        if telegram_link and getattr(exc, "blocked", False):
            # Cliente bloqueou o bot: encerra o canal para ele e não tenta de novo.
            telegram_link.status = "PARADO"
            telegram_link.stopped_at = utcnow()
            recipient.status = "IGNORADO"
            _finish_if_done(db, dispatch)
            return "failed"
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

    # O marco do envio é o aceite da mensagem principal pela Z-API.
    # Não esperar anexos: o cliente pode responder enquanto a imagem é enviada.
    accepted_at = utcnow()
    contact = ensure_contact(db, lead.phone, lead=lead)
    conversation = ensure_open_conversation(
        db,
        contact,
        lead=lead,
        source="TELEGRAM" if telegram else f"DISPARO:{dispatch.id}",
        queue_id=queue.id,
        queue_origin="DISPATCH",
        assign_if_unowned=False,
        zapi_instance_id=zapi_instance.id if zapi_instance else None,
    )
    provider_id = str(
        result.get("messageId") or result.get("id") or ""
    ).strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None

    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="BUTTONS" if quick_replies else "TEXT",
        body=body,
        sender_user_id=dispatch.created_by_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    # Status RECEIVED/READ pode chegar no webhook antes do commit do disparo.
    _reconcile_dispatch_message_status(db, message)

    media_sent = 0
    media_errors: list[str] = []
    attachments = [] if telegram else campaign_media_attachments(campaign)
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
                media_message = register_message(
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
                _reconcile_dispatch_message_status(db, media_message)
                media_sent += 1
            except (MediaStorageError, ZApiError) as exc:
                media_errors.append(f"{file_name}: {exc}")

    now = accepted_at
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
            kind="TELEGRAM" if telegram else "WHATSAPP",
            status="CONCLUIDA",
            channel="TELEGRAM" if telegram else "WHATSAPP",
            outcome=f"DISPARO_{'TELEGRAM' if telegram else 'ZAPI'}:{provider_id or message.id}"[:120],
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
