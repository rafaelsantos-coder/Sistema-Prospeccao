from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AuditLog,
    Contact,
    Conversation,
    ConversationAssignment,
    ConversationQueueLink,
    Message,
    MessageEvent,
    User,
)
from app.services.conversation_engine import apply_conversation_scope
from app.timeutils import LOCAL_TZ, local_input_to_utc, utcnow


OPEN_STATUSES = {
    "NOVO",
    "EM_FILA",
    "ATRIBUIDO",
    "EM_ATENDIMENTO",
    "AGUARDANDO_CLIENTE",
    "AGUARDANDO_INTERNO",
    "FOLLOW_UP",
}
CLOSED_STATUSES = {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}


@dataclass(slots=True)
class ChatReportFilters:
    date_from: str
    date_to: str
    q: str = ""
    phone: str = ""
    operator_id: int | None = None
    branch_id: int | None = None
    team: str = ""
    status: str = ""
    direction: str = ""
    message_type: str = ""
    channel_phone: str = ""
    queue_id: int | None = None

    @classmethod
    def defaults(cls) -> "ChatReportFilters":
        today = datetime.now(LOCAL_TZ).date()
        return cls(
            date_from=(today - timedelta(days=29)).isoformat(),
            date_to=today.isoformat(),
        )

    @property
    def start_utc(self) -> datetime:
        value = local_input_to_utc(f"{self.date_from}T00:00:00")
        return value or (utcnow() - timedelta(days=29))

    @property
    def end_utc(self) -> datetime:
        value = local_input_to_utc(f"{self.date_to}T23:59:59.999999")
        return value or utcnow()

    def as_query_dict(self) -> dict[str, str]:
        values = {
            "date_from": self.date_from,
            "date_to": self.date_to,
            "q": self.q,
            "phone": self.phone,
            "operator_id": str(self.operator_id or ""),
            "branch_id": str(self.branch_id or ""),
            "team": self.team,
            "status": self.status,
            "direction": self.direction,
            "message_type": self.message_type,
            "channel_phone": self.channel_phone,
            "queue_id": str(self.queue_id or ""),
        }
        return {key: value for key, value in values.items() if value}


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    start_value = _utc_naive(start)
    end_value = _utc_naive(end)
    if not start_value or not end_value:
        return None
    seconds = (end_value - start_value).total_seconds()
    return max(0.0, seconds)


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    value = max(0, int(round(float(seconds))))
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}min {sec:02d}s" if sec else f"{minutes}min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}min" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h" if hours else f"{days}d"


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _message_metrics(messages: list[Message]) -> dict[str, Any]:
    ordered = sorted(
        [item for item in messages if item.direction in {"INBOUND", "OUTBOUND"}],
        key=lambda item: (_utc_naive(item.created_at) or datetime.min, item.id or 0),
    )
    waiting_since: datetime | None = None
    first_inbound_at: datetime | None = None
    first_response_seconds: float | None = None
    first_response_user_id: int | None = None
    waits: list[dict[str, Any]] = []

    for item in ordered:
        created = _utc_naive(item.created_at)
        if not created:
            continue
        if item.direction == "INBOUND":
            if first_inbound_at is None:
                first_inbound_at = created
            if waiting_since is None:
                waiting_since = created
            continue

        if item.direction == "OUTBOUND" and waiting_since is not None:
            wait = duration_seconds(waiting_since, created)
            if wait is not None:
                waits.append(
                    {
                        "seconds": wait,
                        "user_id": item.sender_user_id,
                        "message_id": item.id,
                    }
                )
                if first_response_seconds is None:
                    first_response_seconds = wait
                    first_response_user_id = item.sender_user_id
            waiting_since = None

    return {
        "first_response_seconds": first_response_seconds,
        "first_response_user_id": first_response_user_id,
        "reply_waits": waits,
        "avg_reply_seconds": _mean([item["seconds"] for item in waits]),
        "median_reply_seconds": _median([item["seconds"] for item in waits]),
        "unanswered_since": waiting_since,
    }


def conversation_metrics(conversation: Conversation) -> dict[str, Any]:
    metrics = _message_metrics(conversation.messages)
    metrics.update(
        {
            "message_count": len(conversation.messages),
            "inbound_count": sum(1 for item in conversation.messages if item.direction == "INBOUND"),
            "outbound_count": sum(1 for item in conversation.messages if item.direction == "OUTBOUND"),
            "internal_count": sum(1 for item in conversation.messages if item.direction == "INTERNAL"),
            "close_seconds": duration_seconds(conversation.opened_at, conversation.closed_at),
        }
    )
    return metrics


def _first_assignment(conversation: Conversation) -> datetime | None:
    assignments = [
        item for item in conversation.assignments
        if item.user_id is not None and item.created_at is not None
    ]
    if not assignments:
        return None
    return min((_utc_naive(item.created_at) for item in assignments if item.created_at), default=None)


def _connected_phones(db: Session, start: datetime, end: datetime) -> list[str]:
    stmt = (
        select(MessageEvent.payload_json)
        .where(
            MessageEvent.created_at >= start,
            MessageEvent.created_at <= end,
            MessageEvent.payload_json.ilike("%connectedPhone%"),
        )
        .order_by(MessageEvent.created_at.desc())
        .limit(5000)
    )
    values: set[str] = set()
    for raw in db.scalars(stmt):
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        phone = re.sub(r"\D", "", str(payload.get("connectedPhone") or ""))
        if phone:
            values.add(phone)

    if values:
        return sorted(values)

    fallback = (
        select(MessageEvent.payload_json)
        .where(MessageEvent.payload_json.ilike("%connectedPhone%"))
        .order_by(MessageEvent.created_at.desc())
        .limit(300)
    )
    for raw in db.scalars(fallback):
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        phone = re.sub(r"\D", "", str(payload.get("connectedPhone") or ""))
        if phone:
            values.add(phone)
    return sorted(values)


def _channel_phone_exists(phone: str, start: datetime, end: datetime):
    digits = re.sub(r"\D", "", phone or "")
    return exists(
        select(MessageEvent.id)
        .join(Message, MessageEvent.message_id == Message.id)
        .where(
            Message.conversation_id == Conversation.id,
            MessageEvent.created_at >= start,
            MessageEvent.created_at <= end,
            MessageEvent.payload_json.ilike(f"%{digits}%"),
        )
    )


def _conversation_query(
    user: User,
    filters: ChatReportFilters,
):
    start = filters.start_utc
    end = filters.end_utc

    period_message_conditions = [
        Message.conversation_id == Conversation.id,
        Message.created_at >= start,
        Message.created_at <= end,
    ]
    if filters.direction:
        period_message_conditions.append(Message.direction == filters.direction)
    if filters.message_type:
        period_message_conditions.append(Message.message_type == filters.message_type)

    message_in_period = exists(select(Message.id).where(*period_message_conditions))
    opened_in_period = and_(
        Conversation.opened_at >= start,
        Conversation.opened_at <= end,
    )

    stmt = apply_conversation_scope(
        select(Conversation)
        .join(Contact, Conversation.contact_id == Contact.id),
        user,
    )

    if filters.direction or filters.message_type:
        stmt = stmt.where(message_in_period)
    else:
        stmt = stmt.where(or_(opened_in_period, message_in_period))

    if filters.q:
        term = f"%{filters.q.strip()}%"
        stmt = stmt.where(
            or_(
                Contact.name.ilike(term),
                Contact.business_name.ilike(term),
                Contact.phone.ilike(term),
                Conversation.last_message_preview.ilike(term),
            )
        )
    if filters.phone:
        digits = re.sub(r"\D", "", filters.phone)
        if digits:
            stmt = stmt.where(Contact.phone.ilike(f"%{digits}%"))
    if filters.branch_id:
        stmt = stmt.where(Conversation.branch_id == filters.branch_id)
    if filters.team:
        stmt = stmt.where(Conversation.team == filters.team)
    if filters.status:
        stmt = stmt.where(Conversation.status == filters.status)
    if filters.operator_id:
        operator_id = filters.operator_id
        historic_assignment = exists(
            select(ConversationAssignment.id).where(
                ConversationAssignment.conversation_id == Conversation.id,
                ConversationAssignment.user_id == operator_id,
            )
        )
        historic_message = exists(
            select(Message.id).where(
                Message.conversation_id == Conversation.id,
                Message.sender_user_id == operator_id,
            )
        )
        stmt = stmt.where(
            or_(
                Conversation.assigned_user_id == operator_id,
                historic_assignment,
                historic_message,
            )
        )
    if filters.channel_phone:
        stmt = stmt.where(
            _channel_phone_exists(filters.channel_phone, start, end)
        )
    if filters.queue_id:
        stmt = stmt.where(
            exists(
                select(ConversationQueueLink.id).where(
                    ConversationQueueLink.conversation_id == Conversation.id,
                    ConversationQueueLink.queue_id == filters.queue_id,
                )
            )
        )

    return (
        stmt.options(
            selectinload(Conversation.contact),
            selectinload(Conversation.assigned_user),
            selectinload(Conversation.branch),
            selectinload(Conversation.messages).selectinload(Message.sender_user),
            selectinload(Conversation.assignments).selectinload(ConversationAssignment.user),
        )
        .order_by(
            Conversation.last_message_at.desc().nullslast(),
            Conversation.opened_at.desc(),
        )
        .limit(5000)
    )


def build_chat_report(
    db: Session,
    user: User,
    filters: ChatReportFilters,
) -> dict[str, Any]:
    start = filters.start_utc
    end = filters.end_utc
    conversations = list(db.scalars(_conversation_query(user, filters)).unique())

    rows: list[dict[str, Any]] = []
    all_period_messages: list[Message] = []
    first_reply_values: list[float] = []
    reply_values: list[float] = []
    close_values: list[float] = []
    assignment_values: list[float] = []
    pending_wait_values: list[float] = []
    operator_stats: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "user_id": None,
            "name": "",
            "conversations": set(),
            "outbound_messages": 0,
            "first_reply_values": [],
            "reply_values": [],
        }
    )

    now = utcnow()

    for conversation in conversations:
        period_messages = []
        for message in conversation.messages:
            created = _utc_naive(message.created_at)
            if not created or not (start <= created <= end):
                continue
            if filters.direction and message.direction != filters.direction:
                continue
            if filters.message_type and message.message_type != filters.message_type:
                continue
            period_messages.append(message)
        all_period_messages.extend(period_messages)

        full_metrics = _message_metrics(conversation.messages)
        first_response = full_metrics["first_response_seconds"]
        if first_response is not None:
            first_reply_values.append(first_response)

        reply_waits = full_metrics["reply_waits"]
        reply_values.extend(item["seconds"] for item in reply_waits)

        close_seconds = duration_seconds(conversation.opened_at, conversation.closed_at)
        if close_seconds is not None:
            close_values.append(close_seconds)

        first_assignment_at = _first_assignment(conversation)
        assignment_seconds = duration_seconds(conversation.opened_at, first_assignment_at)
        if assignment_seconds is not None:
            assignment_values.append(assignment_seconds)

        unanswered_since = full_metrics["unanswered_since"]
        pending_wait_seconds = duration_seconds(unanswered_since, now)
        if pending_wait_seconds is not None:
            pending_wait_values.append(pending_wait_seconds)

        for message in period_messages:
            if message.direction != "OUTBOUND" or not message.sender_user_id:
                continue
            stat = operator_stats[message.sender_user_id]
            stat["user_id"] = message.sender_user_id
            stat["name"] = message.sender_user.name if message.sender_user else f"Usuário {message.sender_user_id}"
            stat["conversations"].add(conversation.id)
            stat["outbound_messages"] += 1

        first_user_id = full_metrics["first_response_user_id"]
        if first_user_id and first_response is not None:
            stat = operator_stats[first_user_id]
            stat["user_id"] = first_user_id
            if not stat["name"]:
                user_obj = next(
                    (
                        item.sender_user for item in conversation.messages
                        if item.sender_user_id == first_user_id and item.sender_user
                    ),
                    None,
                )
                stat["name"] = user_obj.name if user_obj else f"Usuário {first_user_id}"
            stat["first_reply_values"].append(first_response)
            stat["conversations"].add(conversation.id)

        for wait in reply_waits:
            responder_id = wait["user_id"]
            if not responder_id:
                continue
            stat = operator_stats[responder_id]
            stat["user_id"] = responder_id
            if not stat["name"]:
                user_obj = next(
                    (
                        item.sender_user for item in conversation.messages
                        if item.sender_user_id == responder_id and item.sender_user
                    ),
                    None,
                )
                stat["name"] = user_obj.name if user_obj else f"Usuário {responder_id}"
            stat["reply_values"].append(wait["seconds"])
            stat["conversations"].add(conversation.id)

        inbound_count = sum(1 for item in period_messages if item.direction == "INBOUND")
        outbound_count = sum(1 for item in period_messages if item.direction == "OUTBOUND")
        internal_count = sum(1 for item in period_messages if item.direction == "INTERNAL")

        rows.append(
            {
                "id": conversation.id,
                "name": (
                    conversation.contact.business_name
                    or conversation.contact.name
                    or conversation.contact.phone
                ),
                "phone": conversation.contact.phone,
                "status": conversation.status,
                "team": conversation.team,
                "branch": conversation.branch.city if conversation.branch else "Não definida",
                "owner": conversation.assigned_user.name if conversation.assigned_user else "Fila sem responsável",
                "opened_at": conversation.opened_at,
                "closed_at": conversation.closed_at,
                "last_message_at": conversation.last_message_at,
                "last_message_preview": conversation.last_message_preview,
                "message_count": len(period_messages),
                "inbound_count": inbound_count,
                "outbound_count": outbound_count,
                "internal_count": internal_count,
                "first_response_seconds": first_response,
                "avg_reply_seconds": full_metrics["avg_reply_seconds"],
                "close_seconds": close_seconds,
                "assignment_seconds": assignment_seconds,
                "pending_wait_seconds": pending_wait_seconds,
            }
        )

    inbound_total = sum(1 for item in all_period_messages if item.direction == "INBOUND")
    outbound_total = sum(1 for item in all_period_messages if item.direction == "OUTBOUND")
    internal_total = sum(1 for item in all_period_messages if item.direction == "INTERNAL")
    delivered_total = sum(
        1 for item in all_period_messages
        if item.direction == "OUTBOUND" and item.status in {"RECEIVED", "READ", "PLAYED"}
    )
    read_total = sum(
        1 for item in all_period_messages
        if item.direction == "OUTBOUND" and item.status in {"READ", "PLAYED"}
    )

    first_reply_sla = {
        "under_5m": sum(1 for value in first_reply_values if value <= 300),
        "under_15m": sum(1 for value in first_reply_values if value <= 900),
        "under_60m": sum(1 for value in first_reply_values if value <= 3600),
        "over_60m": sum(1 for value in first_reply_values if value > 3600),
    }

    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {"conversations": 0, "inbound": 0, "outbound": 0}
    )
    for conversation in conversations:
        opened = _utc_naive(conversation.opened_at)
        if opened and start <= opened <= end:
            local = opened.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            daily[local.date().isoformat()]["conversations"] += 1
    for message in all_period_messages:
        created = _utc_naive(message.created_at)
        if not created:
            continue
        local = created.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        key = local.date().isoformat()
        if message.direction == "INBOUND":
            daily[key]["inbound"] += 1
        elif message.direction == "OUTBOUND":
            daily[key]["outbound"] += 1

    operator_rows = []
    for stat in operator_stats.values():
        operator_rows.append(
            {
                "user_id": stat["user_id"],
                "name": stat["name"],
                "conversations": len(stat["conversations"]),
                "outbound_messages": stat["outbound_messages"],
                "first_response_count": len(stat["first_reply_values"]),
                "avg_first_response_seconds": _mean(stat["first_reply_values"]),
                "avg_reply_seconds": _mean(stat["reply_values"]),
            }
        )
    operator_rows.sort(
        key=lambda item: (-item["conversations"], item["name"].casefold())
    )

    selected_ids = [item.id for item in conversations]
    audit_rows: list[AuditLog] = []
    if selected_ids:
        audit_stmt = (
            select(AuditLog)
            .options(selectinload(AuditLog.user))
            .where(
                AuditLog.created_at >= start,
                AuditLog.created_at <= end,
                AuditLog.entity_type == "conversation",
                AuditLog.entity_id.in_([str(item) for item in selected_ids]),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(250)
        )
        audit_rows = list(db.scalars(audit_stmt))

    return {
        "filters": filters,
        "conversations": rows,
        "conversation_objects": conversations,
        "messages": sorted(
            all_period_messages,
            key=lambda item: (_utc_naive(item.created_at) or datetime.min, item.id),
            reverse=True,
        ),
        "audit_logs": audit_rows,
        "connected_phones": _connected_phones(db, start, end),
        "operators": operator_rows,
        "daily": [
            {"date": key, **value}
            for key, value in sorted(daily.items())
        ],
        "kpis": {
            "conversations": len(conversations),
            "active": sum(1 for item in conversations if item.status in OPEN_STATUSES),
            "closed": sum(1 for item in conversations if item.status in CLOSED_STATUSES),
            "messages": len(all_period_messages),
            "inbound": inbound_total,
            "outbound": outbound_total,
            "internal": internal_total,
            "avg_first_response_seconds": _mean(first_reply_values),
            "median_first_response_seconds": _median(first_reply_values),
            "avg_reply_seconds": _mean(reply_values),
            "median_reply_seconds": _median(reply_values),
            "avg_close_seconds": _mean(close_values),
            "avg_assignment_seconds": _mean(assignment_values),
            "avg_pending_wait_seconds": _mean(pending_wait_values),
            "pending_unanswered": len(pending_wait_values),
            "delivery_rate": (delivered_total / outbound_total * 100) if outbound_total else 0.0,
            "read_rate": (read_total / outbound_total * 100) if outbound_total else 0.0,
            "first_reply_sla": first_reply_sla,
        },
        "truncated": len(conversations) >= 5000,
    }
