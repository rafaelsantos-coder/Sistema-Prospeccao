from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AIFollowupJob,
    AIClosingState,
    AIConversationState,
    AIRun,
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

JOURNEY_STAGE_ORDER = [
    "IDENTIFY_CUSTOMER",
    "CHOOSE_PRODUCT",
    "CHOOSE_SEGMENT",
    "CONFIRM_CITY",
    "SELECT_OFFER",
    "HANDLE_OBJECTION",
    "CONFIRM_PURCHASE",
    "CLOSING",
]
JOURNEY_STAGE_LABELS = {
    "IDENTIFY_CUSTOMER": "Identificar cliente",
    "CHOOSE_PRODUCT": "Escolher produto",
    "CHOOSE_SEGMENT": "Casa ou empresa",
    "CONFIRM_CITY": "Confirmar cidade",
    "SELECT_OFFER": "Selecionar oferta",
    "HANDLE_OBJECTION": "Tratar objeção",
    "CONFIRM_PURCHASE": "Confirmar compra",
    "CLOSING": "Vendedor IA",
    "HUMAN_HANDOFF": "Handoff humano",
    "OPT_OUT": "Opt-out",
    "COMPLETED": "Venda concluída",
}


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


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_ai_journey_report(
    db: Session,
    *,
    conversations: list[Conversation],
    conversation_ids: list[int],
    ai_state_map: dict[int, AIConversationState],
    closing_state_map: dict[int, AIClosingState],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    empty_stages = [
        {
            "code": code,
            "label": JOURNEY_STAGE_LABELS[code],
            "conversations": 0,
            "interactions": 0,
            "avg_interactions": 0.0,
            "current": 0,
            "stalled": 0,
        }
        for code in JOURNEY_STAGE_ORDER
    ]
    empty = {
        "kpis": {
            "ai_conversations": 0,
            "offer_defined": 0,
            "objection_conversations": 0,
            "purchase_acceptances": 0,
            "closer_started_period": 0,
            "completed_period": 0,
            "human_handoffs": 0,
            "stalled": 0,
            "at_risk": 0,
            "avg_interactions_per_conversation": 0.0,
            "avg_interactions_to_acceptance": None,
            "acceptance_rate": 0.0,
            "closer_start_rate": 0.0,
            "completion_rate": 0.0,
            "bottleneck_code": "",
            "bottleneck_label": "Sem gargalo identificado",
            "bottleneck_current": 0,
        },
        "funnel": [],
        "stages": empty_stages,
        "stalled_conversations": [],
        "current_by_conversation": {},
        "period_run_count": 0,
    }
    if not conversation_ids:
        return empty

    period_runs = list(
        db.scalars(
            select(AIRun)
            .where(
                AIRun.conversation_id.in_(conversation_ids),
                AIRun.status == "CONCLUIDO",
                AIRun.created_at >= start,
                AIRun.created_at <= end,
            )
            .order_by(AIRun.conversation_id.asc(), AIRun.id.asc())
        )
    )

    journey_runs: list[tuple[AIRun, dict[str, Any]]] = []
    stage_conversations: dict[str, set[int]] = defaultdict(set)
    stage_interactions: dict[str, int] = defaultdict(int)
    conversation_stage_interactions: dict[tuple[int, str], int] = defaultdict(int)
    runs_by_conversation: dict[int, list[tuple[AIRun, dict[str, Any]]]] = defaultdict(list)

    ai_conversation_ids: set[int] = set()
    offer_defined_ids: set[int] = set()
    objection_ids: set[int] = set()
    accepted_ids: set[int] = set()
    human_handoff_ids: set[int] = set()

    for run in period_runs:
        payload = _json_dict(run.output_json)
        stage = str(payload.get("journey_state") or "").upper()
        if stage not in JOURNEY_STAGE_LABELS:
            continue

        journey_runs.append((run, payload))
        runs_by_conversation[run.conversation_id].append((run, payload))
        ai_conversation_ids.add(run.conversation_id)

        if stage in JOURNEY_STAGE_ORDER:
            stage_conversations[stage].add(run.conversation_id)
            stage_interactions[stage] += 1
            conversation_stage_interactions[(run.conversation_id, stage)] += 1

        if (
            str(payload.get("offer_plan_code") or "").strip()
            or payload.get("offer_price") is not None
        ):
            offer_defined_ids.add(run.conversation_id)

        objection = str(payload.get("objection") or "NONE").upper()
        if objection not in {"", "NONE"}:
            objection_ids.add(run.conversation_id)

        handoff_target = str(payload.get("handoff_target") or "NONE").upper()
        if stage == "CLOSING" or handoff_target == "CLOSER_AI":
            accepted_ids.add(run.conversation_id)
        if stage == "HUMAN_HANDOFF" or handoff_target == "HUMAN":
            human_handoff_ids.add(run.conversation_id)

    latest_journey_run_map: dict[int, AIRun] = {}
    latest_journey_payload_map: dict[int, dict[str, Any]] = {}
    latest_journey_ids = list(
        db.execute(
            select(
                AIRun.conversation_id,
                func.max(AIRun.id).label("run_id"),
            )
            .where(
                AIRun.conversation_id.in_(conversation_ids),
                AIRun.status == "CONCLUIDO",
                AIRun.output_json.ilike('%"journey_state"%'),
            )
            .group_by(AIRun.conversation_id)
        )
    )
    run_ids = [int(row.run_id) for row in latest_journey_ids if row.run_id]
    if run_ids:
        latest_journey_run_map = {
            item.conversation_id: item
            for item in db.scalars(select(AIRun).where(AIRun.id.in_(run_ids)))
        }
        latest_journey_payload_map = {
            conversation_id: _json_dict(run.output_json)
            for conversation_id, run in latest_journey_run_map.items()
        }

    closing_started_ids: set[int] = set()
    completed_period_ids: set[int] = set()
    for closing in closing_state_map.values():
        created = _utc_naive(closing.created_at)
        completed = _utc_naive(closing.completed_at)
        if created and start <= created <= end:
            closing_started_ids.add(closing.conversation_id)
        if completed and start <= completed <= end:
            completed_period_ids.add(closing.conversation_id)

    current_stage_counts: dict[str, int] = defaultdict(int)
    stalled_stage_counts: dict[str, int] = defaultdict(int)
    current_by_conversation: dict[int, dict[str, Any]] = {}
    stalled_conversations: list[dict[str, Any]] = []
    at_risk = 0
    conversation_map = {item.id: item for item in conversations}

    for conversation_id in conversation_ids:
        ai_state = ai_state_map.get(conversation_id)
        closing = closing_state_map.get(conversation_id)
        payload = dict(latest_journey_payload_map.get(conversation_id) or {})

        stage = str(payload.get("journey_state") or "").upper()
        pending_action = str(payload.get("journey_pending_action") or "").strip()
        try:
            stagnation_count = max(0, int(payload.get("stagnation_count") or 0))
        except (TypeError, ValueError):
            stagnation_count = 0

        if closing and closing.step == "COMPLETED":
            stage = "COMPLETED"
            pending_action = ""
            stagnation_count = 0
        elif (
            (closing and closing.step == "ESCALATED")
            or (ai_state and ai_state.state in {"HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "ESCALATED"})
        ):
            stage = "HUMAN_HANDOFF"
            pending_action = ""
            stagnation_count = 0
        elif closing and ai_state and ai_state.state == "CLOSING":
            stage = "CLOSING"
            pending_action = (
                f"Vendedor IA · etapa {closing.step.replace('_', ' ').title()}"
                if closing.step
                else "Vendedor IA em fechamento"
            )
            stagnation_count = 0

        if stage:
            current_stage_counts[stage] += 1

        current = {
            "code": stage,
            "label": JOURNEY_STAGE_LABELS.get(stage, stage.replace("_", " ").title() if stage else ""),
            "pending_action": pending_action,
            "stagnation_count": stagnation_count,
        }
        current_by_conversation[conversation_id] = current

        if stage in JOURNEY_STAGE_ORDER[:-1] and stagnation_count >= 2:
            at_risk += 1
        if stage in JOURNEY_STAGE_ORDER[:-1] and stagnation_count >= 3:
            stalled_stage_counts[stage] += 1
            conversation = conversation_map.get(conversation_id)
            if conversation:
                stalled_conversations.append(
                    {
                        "id": conversation.id,
                        "name": (
                            conversation.contact.business_name
                            or conversation.contact.name
                            or conversation.contact.phone
                        ),
                        "phone": conversation.contact.phone,
                        "stage_code": stage,
                        "stage_label": JOURNEY_STAGE_LABELS.get(stage, stage),
                        "pending_action": pending_action,
                        "stagnation_count": stagnation_count,
                        "interactions_in_stage": conversation_stage_interactions.get(
                            (conversation_id, stage),
                            0,
                        ),
                        "last_message_at": conversation.last_message_at,
                    }
                )

    stalled_conversations.sort(
        key=lambda item: (
            -int(item["stagnation_count"]),
            str(item["name"]).casefold(),
        )
    )

    stage_rows: list[dict[str, Any]] = []
    for code in JOURNEY_STAGE_ORDER:
        conversations_in_stage = len(stage_conversations.get(code, set()))
        interactions = int(stage_interactions.get(code, 0))
        stage_rows.append(
            {
                "code": code,
                "label": JOURNEY_STAGE_LABELS[code],
                "conversations": conversations_in_stage,
                "interactions": interactions,
                "avg_interactions": (
                    interactions / conversations_in_stage
                    if conversations_in_stage
                    else 0.0
                ),
                "current": int(current_stage_counts.get(code, 0)),
                "stalled": int(stalled_stage_counts.get(code, 0)),
            }
        )

    turns_to_acceptance: list[float] = []
    for conversation_id in accepted_ids:
        runs = runs_by_conversation.get(conversation_id, [])
        turns = 0
        for _run, payload in runs:
            turns += 1
            stage = str(payload.get("journey_state") or "").upper()
            target = str(payload.get("handoff_target") or "NONE").upper()
            if stage == "CLOSING" or target == "CLOSER_AI":
                turns_to_acceptance.append(float(turns))
                break

    bottleneck_candidates = [
        row for row in stage_rows
        if row["code"] not in {"CLOSING"}
    ]
    bottleneck = max(
        bottleneck_candidates,
        key=lambda row: (row["current"], row["stalled"], row["interactions"]),
        default=None,
    )
    if not bottleneck or bottleneck["current"] <= 0:
        bottleneck = None

    acceptance_rate = (
        len(accepted_ids) / len(offer_defined_ids) * 100
        if offer_defined_ids
        else 0.0
    )
    closer_start_rate = (
        len(closing_started_ids & accepted_ids) / len(accepted_ids) * 100
        if accepted_ids
        else 0.0
    )
    completed_from_started_ids = completed_period_ids & closing_started_ids
    completion_rate = (
        len(completed_from_started_ids) / len(closing_started_ids) * 100
        if closing_started_ids
        else 0.0
    )

    funnel_closer_ids = closing_started_ids & accepted_ids
    funnel_completed_ids = completed_period_ids & funnel_closer_ids
    funnel_values = [
        ("SDR IA", len(ai_conversation_ids)),
        ("Oferta definida", len(offer_defined_ids)),
        ("Aceite explícito", len(accepted_ids)),
        ("Vendedor IA", len(funnel_closer_ids)),
        ("Venda concluída", len(funnel_completed_ids)),
    ]
    funnel_base = max(1, funnel_values[0][1])
    funnel = [
        {
            "label": label,
            "value": value,
            "pct": value / funnel_base * 100,
        }
        for label, value in funnel_values
    ]

    empty["kpis"].update(
        {
            "ai_conversations": len(ai_conversation_ids),
            "offer_defined": len(offer_defined_ids),
            "objection_conversations": len(objection_ids),
            "purchase_acceptances": len(accepted_ids),
            "closer_started_period": len(closing_started_ids),
            "completed_period": len(completed_period_ids),
            "human_handoffs": len(human_handoff_ids),
            "stalled": len(stalled_conversations),
            "at_risk": at_risk,
            "avg_interactions_per_conversation": (
                len(journey_runs) / len(ai_conversation_ids)
                if ai_conversation_ids
                else 0.0
            ),
            "avg_interactions_to_acceptance": _mean(turns_to_acceptance),
            "acceptance_rate": acceptance_rate,
            "closer_start_rate": closer_start_rate,
            "completion_rate": completion_rate,
            "bottleneck_code": bottleneck["code"] if bottleneck else "",
            "bottleneck_label": bottleneck["label"] if bottleneck else "Sem gargalo identificado",
            "bottleneck_current": int(bottleneck["current"]) if bottleneck else 0,
        }
    )
    empty["funnel"] = funnel
    empty["stages"] = stage_rows
    empty["stalled_conversations"] = stalled_conversations[:50]
    empty["current_by_conversation"] = current_by_conversation
    empty["period_run_count"] = len(journey_runs)
    return empty


def _previous_period_filters(filters: ChatReportFilters) -> ChatReportFilters:
    try:
        current_from = datetime.fromisoformat(filters.date_from).date()
        current_to = datetime.fromisoformat(filters.date_to).date()
    except (TypeError, ValueError):
        current_to = datetime.now(LOCAL_TZ).date()
        current_from = current_to - timedelta(days=29)

    span_days = max(1, (current_to - current_from).days + 1)
    previous_to = current_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=span_days - 1)
    return replace(
        filters,
        date_from=previous_from.isoformat(),
        date_to=previous_to.isoformat(),
    )


def _ai_state_maps(
    db: Session,
    conversation_ids: list[int],
) -> tuple[dict[int, AIConversationState], dict[int, AIClosingState]]:
    if not conversation_ids:
        return {}, {}
    ai_state_map = {
        item.conversation_id: item
        for item in db.scalars(
            select(AIConversationState).where(
                AIConversationState.conversation_id.in_(conversation_ids)
            )
        )
    }
    closing_state_map = {
        item.conversation_id: item
        for item in db.scalars(
            select(AIClosingState).where(
                AIClosingState.conversation_id.in_(conversation_ids)
            )
        )
    }
    return ai_state_map, closing_state_map


_STAGE_RECOMMENDATIONS = {
    "IDENTIFY_CUSTOMER": (
        "Simplificar a abertura e manter apenas a pergunta cliente/não cliente com botões Sim e Não."
    ),
    "CHOOSE_PRODUCT": (
        "Revisar a CTA de escolha entre Internet e Telefonia Móvel e garantir botões curtos e objetivos."
    ),
    "CHOOSE_SEGMENT": (
        "Manter a escolha Casa/Empresa direta, sem texto adicional que desvie a resposta."
    ),
    "CONFIRM_CITY": (
        "Revisar o reconhecimento da cidade e usar uma pergunta curta quando ela ainda não estiver no contexto."
    ),
    "SELECT_OFFER": (
        "Revisar clareza da oferta, preço, velocidade e CTA. Evitar excesso de detalhes antes da escolha."
    ),
    "HANDLE_OBJECTION": (
        "Variar o argumento conforme preço, qualidade ou momento e evitar repetir a mesma oferta sem diagnóstico."
    ),
    "CONFIRM_PURCHASE": (
        "Usar uma CTA direta de contratação e não voltar para explicações já concluídas."
    ),
    "CLOSING": (
        "Revisar disponibilidade do Vendedor IA, CRM e continuidade do fechamento."
    ),
}


def _metric_delta(current: float | int | None, previous: float | int | None) -> float | None:
    if current is None or previous is None:
        return None
    return float(current) - float(previous)


def _build_ai_performance_management(
    current: dict[str, Any],
    previous: dict[str, Any],
    *,
    current_label: str,
    previous_label: str,
) -> dict[str, Any]:
    ck = current["kpis"]
    pk = previous["kpis"]
    ai_total = int(ck.get("ai_conversations") or 0)
    offers = int(ck.get("offer_defined") or 0)
    acceptances = int(ck.get("purchase_acceptances") or 0)
    closer_started = int(ck.get("closer_started_period") or 0)
    stalled = int(ck.get("stalled") or 0)
    human_handoffs = int(ck.get("human_handoffs") or 0)

    acceptance_delta = _metric_delta(
        ck.get("acceptance_rate"),
        pk.get("acceptance_rate"),
    )
    closer_delta = _metric_delta(
        ck.get("closer_start_rate"),
        pk.get("closer_start_rate"),
    )
    completion_delta = _metric_delta(
        ck.get("completion_rate"),
        pk.get("completion_rate"),
    )
    interactions_delta = _metric_delta(
        ck.get("avg_interactions_to_acceptance"),
        pk.get("avg_interactions_to_acceptance"),
    )

    alerts: list[dict[str, Any]] = []

    def add_alert(
        severity: str,
        code: str,
        title: str,
        detail: str,
        recommendation: str,
        *,
        metric: str = "",
        value: float | int | None = None,
        delta: float | None = None,
    ) -> None:
        alerts.append(
            {
                "severity": severity,
                "code": code,
                "title": title,
                "detail": detail,
                "recommendation": recommendation,
                "metric": metric,
                "value": value,
                "delta": delta,
            }
        )

    stalled_ratio = (stalled / ai_total * 100) if ai_total else 0.0
    if stalled:
        severity = "CRITICAL" if stalled_ratio >= 15 or stalled >= 5 else "WARNING"
        add_alert(
            severity,
            "STALLED_CONVERSATIONS",
            "Há conversas travadas na jornada",
            (
                f"{stalled} conversa(s) estão com 3 ou mais interações sem avanço "
                f"({stalled_ratio:.1f}% das conversas do SDR IA)."
            ),
            "Abrir primeiro as conversas travadas e atacar a pendência exibida em cada uma.",
            metric="stalled_rate",
            value=stalled_ratio,
        )

    bottleneck_code = str(ck.get("bottleneck_code") or "")
    bottleneck_current = int(ck.get("bottleneck_current") or 0)
    stage_lookup = {
        row["code"]: row
        for row in current.get("stages", [])
    }
    bottleneck_row = stage_lookup.get(bottleneck_code)
    if bottleneck_code and bottleneck_current and bottleneck_row:
        avg_interactions = float(bottleneck_row.get("avg_interactions") or 0.0)
        stage_stalled = int(bottleneck_row.get("stalled") or 0)
        if avg_interactions >= 2.0 or stage_stalled:
            add_alert(
                "WARNING",
                "JOURNEY_BOTTLENECK",
                f"Gargalo em {bottleneck_row['label']}",
                (
                    f"{bottleneck_current} conversa(s) estão atualmente nessa etapa, "
                    f"com média de {avg_interactions:.2f} interação(ões) por conversa."
                ),
                _STAGE_RECOMMENDATIONS.get(
                    bottleneck_code,
                    "Revisar a pergunta, a CTA e a regra de avanço desta etapa.",
                ),
                metric="stage_avg_interactions",
                value=avg_interactions,
            )

    previous_offers = int(pk.get("offer_defined") or 0)
    acceptance_rate = float(ck.get("acceptance_rate") or 0.0)
    if offers >= 5 and acceptance_rate < 25:
        add_alert(
            "CRITICAL" if acceptance_rate < 15 else "WARNING",
            "LOW_ACCEPTANCE_RATE",
            "Conversão de oferta para aceite está baixa",
            (
                f"Apenas {acceptance_rate:.1f}% das conversas com oferta definida "
                "chegaram a um aceite explícito."
            ),
            "Revisar apresentação da oferta, CTA de fechamento e tratamento das principais objeções.",
            metric="acceptance_rate",
            value=acceptance_rate,
            delta=acceptance_delta,
        )
    if (
        offers >= 5
        and previous_offers >= 5
        and acceptance_delta is not None
        and acceptance_delta <= -10
    ):
        add_alert(
            "CRITICAL",
            "ACCEPTANCE_RATE_DROP",
            "A taxa de aceite caiu em relação ao período anterior",
            (
                f"A conversão oferta → aceite caiu {abs(acceptance_delta):.1f} p.p. "
                f"comparando {current_label} com {previous_label}."
            ),
            "Comparar as conversas recentes com as do período anterior e revisar oferta, objeções e CTA que mais mudaram.",
            metric="acceptance_rate",
            value=acceptance_rate,
            delta=acceptance_delta,
        )

    closer_rate = float(ck.get("closer_start_rate") or 0.0)
    if acceptances >= 3 and closer_rate < 90:
        add_alert(
            "CRITICAL",
            "CLOSER_HANDOFF_LOSS",
            "Aceites não estão chegando ao Vendedor IA",
            (
                f"{closer_rate:.1f}% dos aceites explícitos iniciaram o Vendedor IA "
                "dentro do período analisado."
            ),
            "Auditar handoff, disponibilidade do closer e criação do estado de fechamento para não perder cliente já decidido.",
            metric="closer_start_rate",
            value=closer_rate,
            delta=closer_delta,
        )

    completion_rate = float(ck.get("completion_rate") or 0.0)
    if closer_started >= 3 and completion_rate < 60:
        add_alert(
            "CRITICAL" if completion_rate < 35 else "WARNING",
            "LOW_CLOSING_COMPLETION",
            "Fechamentos iniciados estão concluindo pouco",
            (
                f"{completion_rate:.1f}% dos fechamentos iniciados no período "
                "também foram concluídos na mesma coorte."
            ),
            "Revisar em quais etapas do Vendedor IA as contratações estão parando: cadastro, crédito, endereço, plano, faturamento ou assinatura.",
            metric="completion_rate",
            value=completion_rate,
            delta=completion_delta,
        )
    previous_closer_started = int(pk.get("closer_started_period") or 0)
    if (
        closer_started >= 3
        and previous_closer_started >= 3
        and completion_delta is not None
        and completion_delta <= -10
    ):
        add_alert(
            "WARNING",
            "COMPLETION_RATE_DROP",
            "A conclusão do Vendedor IA caiu",
            (
                f"A taxa de conclusão caiu {abs(completion_delta):.1f} p.p. "
                "em relação ao período anterior."
            ),
            "Comparar as etapas de fechamento com maior permanência e verificar erros recentes do CRM ou assinatura.",
            metric="completion_rate",
            value=completion_rate,
            delta=completion_delta,
        )

    interactions_to_acceptance = ck.get("avg_interactions_to_acceptance")
    if (
        acceptances >= 3
        and interactions_to_acceptance is not None
        and float(interactions_to_acceptance) > 5
    ):
        add_alert(
            "WARNING",
            "TOO_MANY_INTERACTIONS",
            "O SDR está demorando para chegar ao aceite",
            (
                f"São necessárias, em média, {float(interactions_to_acceptance):.1f} "
                "interações do SDR até o aceite explícito."
            ),
            "Encurtar perguntas, evitar recapitulações e apresentar oferta/CTA assim que as informações obrigatórias estiverem completas.",
            metric="avg_interactions_to_acceptance",
            value=float(interactions_to_acceptance),
            delta=interactions_delta,
        )

    handoff_ratio = (human_handoffs / ai_total * 100) if ai_total else 0.0
    if ai_total >= 5 and handoff_ratio > 20:
        add_alert(
            "WARNING",
            "HIGH_HUMAN_HANDOFF",
            "Handoff humano acima do esperado",
            (
                f"{handoff_ratio:.1f}% das conversas do SDR IA tiveram destino humano "
                "no período."
            ),
            "Revisar motivos dos handoffs e separar pedidos explícitos de humano de falhas operacionais ou classificações indevidas.",
            metric="human_handoff_rate",
            value=handoff_ratio,
        )

    objection_ratio = (
        int(ck.get("objection_conversations") or 0) / ai_total * 100
        if ai_total
        else 0.0
    )
    if ai_total >= 5 and objection_ratio >= 50:
        add_alert(
            "INFO",
            "HIGH_OBJECTION_VOLUME",
            "Objeções estão concentradas em muitas conversas",
            f"{objection_ratio:.1f}% das conversas do SDR IA registraram alguma objeção.",
            "Usar as conversas com objeção para revisar argumentos comerciais e identificar se preço, qualidade ou momento estão predominando.",
            metric="objection_rate",
            value=objection_ratio,
        )

    severity_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "POSITIVE": 3}
    alerts.sort(key=lambda item: (severity_rank.get(item["severity"], 9), item["title"]))

    score = 100
    score -= sum(18 for item in alerts if item["severity"] == "CRITICAL")
    score -= sum(9 for item in alerts if item["severity"] == "WARNING")
    score -= sum(3 for item in alerts if item["severity"] == "INFO")
    if ai_total and stalled_ratio >= 15:
        score -= 8
    score = max(0, min(100, score))

    if ai_total < 3:
        status = "SEM_BASE"
        status_label = "Base insuficiente"
    elif score >= 85:
        status = "EXCELLENT"
        status_label = "Excelente"
    elif score >= 70:
        status = "GOOD"
        status_label = "Boa"
    elif score >= 50:
        status = "ATTENTION"
        status_label = "Atenção"
    else:
        status = "CRITICAL"
        status_label = "Crítica"

    if not alerts and ai_total >= 3:
        add_alert(
            "POSITIVE",
            "HEALTHY_JOURNEY",
            "Jornada sem alerta relevante",
            "Os principais indicadores estão dentro das faixas operacionais definidas.",
            "Continuar monitorando a evolução por etapa e a comparação com o período anterior.",
        )

    primary = next(
        (item for item in alerts if item["severity"] in {"CRITICAL", "WARNING"}),
        alerts[0] if alerts else None,
    )

    return {
        "score": score,
        "status": status,
        "status_label": status_label,
        "alerts": alerts[:10],
        "critical_count": sum(1 for item in alerts if item["severity"] == "CRITICAL"),
        "warning_count": sum(1 for item in alerts if item["severity"] == "WARNING"),
        "primary_recommendation": primary["recommendation"] if primary else "",
        "comparison": {
            "current_label": current_label,
            "previous_label": previous_label,
            "acceptance_rate_delta": acceptance_delta,
            "closer_start_rate_delta": closer_delta,
            "completion_rate_delta": completion_delta,
            "interactions_to_acceptance_delta": interactions_delta,
            "previous_acceptance_rate": float(pk.get("acceptance_rate") or 0.0),
            "previous_closer_start_rate": float(pk.get("closer_start_rate") or 0.0),
            "previous_completion_rate": float(pk.get("completion_rate") or 0.0),
        },
    }


def build_chat_report(
    db: Session,
    user: User,
    filters: ChatReportFilters,
) -> dict[str, Any]:
    start = filters.start_utc
    end = filters.end_utc
    conversations = list(db.scalars(_conversation_query(user, filters)).unique())
    conversation_ids = [item.id for item in conversations]

    ai_state_map: dict[int, AIConversationState] = {}
    closing_state_map: dict[int, AIClosingState] = {}
    latest_run_map: dict[int, AIRun] = {}
    pending_followup_map: dict[int, int] = {}

    if conversation_ids:
        ai_state_map = {
            item.conversation_id: item
            for item in db.scalars(
                select(AIConversationState).where(
                    AIConversationState.conversation_id.in_(conversation_ids)
                )
            )
        }
        closing_state_map = {
            item.conversation_id: item
            for item in db.scalars(
                select(AIClosingState).where(
                    AIClosingState.conversation_id.in_(conversation_ids)
                )
            )
        }

        latest_run_ids = list(
            db.execute(
                select(
                    AIRun.conversation_id,
                    func.max(AIRun.id).label("run_id"),
                )
                .where(
                    AIRun.conversation_id.in_(conversation_ids),
                    AIRun.status == "CONCLUIDO",
                )
                .group_by(AIRun.conversation_id)
            )
        )
        run_ids = [int(row.run_id) for row in latest_run_ids if row.run_id]
        if run_ids:
            latest_run_map = {
                item.conversation_id: item
                for item in db.scalars(
                    select(AIRun).where(AIRun.id.in_(run_ids))
                )
            }

        pending_followup_map = {
            int(row.conversation_id): int(row.total)
            for row in db.execute(
                select(
                    AIFollowupJob.conversation_id,
                    func.count(AIFollowupJob.id).label("total"),
                )
                .where(
                    AIFollowupJob.conversation_id.in_(conversation_ids),
                    AIFollowupJob.status.in_(["PENDENTE", "PROCESSANDO"]),
                )
                .group_by(AIFollowupJob.conversation_id)
            )
        }

    journey_report = _build_ai_journey_report(
        db,
        conversations=conversations,
        conversation_ids=conversation_ids,
        ai_state_map=ai_state_map,
        closing_state_map=closing_state_map,
        start=start,
        end=end,
    )

    previous_filters = _previous_period_filters(filters)
    previous_conversations = list(
        db.scalars(_conversation_query(user, previous_filters)).unique()
    )
    previous_conversation_ids = [item.id for item in previous_conversations]
    previous_ai_state_map, previous_closing_state_map = _ai_state_maps(
        db,
        previous_conversation_ids,
    )
    previous_journey = _build_ai_journey_report(
        db,
        conversations=previous_conversations,
        conversation_ids=previous_conversation_ids,
        ai_state_map=previous_ai_state_map,
        closing_state_map=previous_closing_state_map,
        start=previous_filters.start_utc,
        end=previous_filters.end_utc,
    )
    journey_report["performance"] = _build_ai_performance_management(
        journey_report,
        previous_journey,
        current_label=f"{filters.date_from} a {filters.date_to}",
        previous_label=(
            f"{previous_filters.date_from} a {previous_filters.date_to}"
        ),
    )
    journey_report["previous_period"] = {
        "date_from": previous_filters.date_from,
        "date_to": previous_filters.date_to,
        "kpis": previous_journey["kpis"],
    }
    journey_current_map = journey_report["current_by_conversation"]

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

        ai_state = ai_state_map.get(conversation.id)
        closing_state = closing_state_map.get(conversation.id)
        latest_run = latest_run_map.get(conversation.id)
        journey_current = journey_current_map.get(conversation.id, {})

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
                "ai_state": ai_state.state if ai_state else "",
                "ai_temperature": ai_state.temperature if ai_state else "",
                "ai_last_intent": ai_state.last_intent if ai_state else "",
                "ai_summary": ai_state.summary if ai_state else "",
                "closing_step": closing_state.step if closing_state else "",
                "closing_error": closing_state.last_error if closing_state else "",
                "pending_followups": pending_followup_map.get(conversation.id, 0),
                "last_ai_run_summary": latest_run.summary if latest_run else "",
                "last_ai_run_intent": latest_run.intent if latest_run else "",
                "journey_state": journey_current.get("code", ""),
                "journey_label": journey_current.get("label", ""),
                "journey_pending_action": journey_current.get("pending_action", ""),
                "journey_stagnation_count": journey_current.get("stagnation_count", 0),
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
        "ai_journey": journey_report,
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
            "ai_active": sum(
                1 for item in ai_state_map.values()
                if item.state == "AI_ACTIVE"
            ),
            "ai_closing": sum(
                1 for item in ai_state_map.values()
                if item.state == "CLOSING"
            ),
            "ai_handoff": sum(
                1 for item in ai_state_map.values()
                if item.state in {"HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "ESCALATED"}
            ),
            "ai_pending_followups": sum(pending_followup_map.values()),
        },
        "truncated": len(conversations) >= 5000,
    }
