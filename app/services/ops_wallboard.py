"""Painel operacional em tempo quase real — visão gerencial de filas e conversas.

Agrega sinais que o sistema já produz (conversas, filas, presença, jobs IA,
fechamento, tags comerciais e auditoria de alertas) em um snapshot único para
gestores e supervisores. Não inventa domínio novo: só consolida.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

LOG = logging.getLogger("sulnet_ops_wallboard")

from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIJob,
    AuditLog,
    Contact,
    Conversation,
    ConversationCommercialTag,
    ConversationQueueLink,
    Message,
    ServiceQueue,
    ServiceQueueMember,
    User,
    WhatsAppPresence,
)
from app.services.conversation_engine import OPEN_CONVERSATION_STATUSES
from app.services.ops_alerts import ALERT_ACTION, STUCK_SALE_AFTER, STUCK_SALE_WINDOW
from app.services.queue_routing import available_queues_for_user
from app.services.whatsapp_presence import online_user_ids
from app.timeutils import local_day_start_utc, utcnow

_CLOSED = {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
_SALE_DONE = {"COMPLETED", "ESCALATED"}
_AI_OWNED = {"AI_ACTIVE", "CLOSING"}
_RISK_TAGS = {"RISCO_PERDA", "DOCUMENTOS_PENDENTES", "ASSINATURA_PENDENTE"}
_STATUS_LABELS = {
    "NOVO": "Novo",
    "EM_FILA": "Em fila",
    "ATRIBUIDO": "Atribuído",
    "EM_ATENDIMENTO": "Em atendimento",
    "AGUARDANDO_CLIENTE": "Aguardando cliente",
    "AGUARDANDO_INTERNO": "Aguardando interno",
    "FOLLOW_UP": "Follow-up",
    "RESOLVIDO": "Resolvido",
    "ENCERRADO": "Encerrado",
    "BLOQUEADO": "Bloqueado",
}
_STEP_LABELS = {
    "NAME": "Nome",
    "PHONE": "Telefone",
    "DOCUMENT": "Documento",
    "ADDRESS": "Endereço",
    "CREDIT": "Crédito",
    "CONTRACT": "Contrato",
    "WAITING_SIGNATURE": "Aguardando assinatura",
    "WAITING_CRM_READY": "Aguardando CRM",
    "COMPLETED": "Concluído",
    "CREDIT_REJECTED": "Crédito reprovado",
    "ESCALATED": "Escalado",
}


def _is_ai_user(ai_user_ids: set[int], user_id: int | None) -> bool:
    return bool(user_id) and user_id in ai_user_ids


def _naive(value):
    """Normaliza datetime aware/naive para comparação UTC sem tzinfo."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value


def _parse_collected(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _won_today(state: AIClosingState, day_start) -> bool:
    if state.step != "COMPLETED" or not state.completed_at:
        return False
    completed = _naive(state.completed_at)
    if completed is None or completed < day_start:
        return False
    return bool(_parse_collected(state.collected_json).get("_won_after_signature"))


def build_ops_wallboard(db: Session, user: User, *, queue_id: int | None = None) -> dict[str, Any]:
    now = utcnow()
    day_start = local_day_start_utc()
    ai_user_ids = {
        uid
        for uid in db.scalars(
            select(AIAgentProfile.user_id).where(
                AIAgentProfile.active.is_(True),
                AIAgentProfile.user_id.is_not(None),
            )
        )
        if uid
    }
    online = set(online_user_ids(db)) | ai_user_ids

    all_queues = [item for item in available_queues_for_user(db, user) if item.active]
    queues = list(all_queues)
    if queue_id is not None:
        queues = [item for item in queues if item.id == queue_id]
        if not queues:
            empty = _empty_payload(now, day_start)
            empty["filters"] = {
                "queue_id": queue_id,
                "queues": [{"id": item.id, "name": item.name} for item in all_queues],
            }
            return empty

    queue_ids = [item.id for item in queues]

    open_stmt = (
        select(Conversation)
        .options(selectinload(Conversation.assigned_user), selectinload(Conversation.contact))
        .where(Conversation.status.in_(OPEN_CONVERSATION_STATUSES))
    )
    if queue_ids:
        open_stmt = open_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id.in_(queue_ids))
    else:
        # Sem filas visíveis: não inventar universo global.
        open_stmt = open_stmt.where(False)

    open_conversations = list(db.scalars(open_stmt).unique())
    open_ids = [item.id for item in open_conversations]

    filter_queue_ids = queue_ids if (queue_ids or queue_id is not None) else None
    started_today = int(db.scalar(_count_opened_today(day_start, filter_queue_ids)) or 0)

    ai_states = {}
    if open_ids:
        for state in db.scalars(
            select(AIConversationState).where(AIConversationState.conversation_id.in_(open_ids))
        ):
            ai_states[state.conversation_id] = state.state

    closing_cutoff = max(now - STUCK_SALE_WINDOW, day_start - timedelta(days=1))
    closing_stmt = (
        select(AIClosingState)
        .where(
            AIClosingState.completed_at.is_(None),
            AIClosingState.updated_at >= closing_cutoff,
        )
        .order_by(AIClosingState.updated_at.desc())
        .limit(250)
    )
    if filter_queue_ids is not None:
        closing_stmt = closing_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == AIClosingState.conversation_id,
        ).where(ConversationQueueLink.queue_id.in_(filter_queue_ids or [-1]))
    closing_states = list(db.scalars(closing_stmt).unique())

    won_today = _safe_call(
        "won_today",
        lambda: resolve_won_today(db, day_start, filter_queue_ids),
        default=0,
    )
    failed_jobs = _safe_call(
        "failed_jobs",
        lambda: _failed_ai_jobs(db, now, filter_queue_ids),
        default=[],
    )
    stuck_sales = _safe_call(
        "stuck_sales",
        lambda: _stuck_sales(db, now, closing_states),
        default=[],
    )
    silent_closings = _safe_call(
        "silent_closings",
        lambda: _silent_closing_ids(db, closing_states),
        default=set(),
    )
    risk_tags = _safe_call(
        "risk_tags",
        lambda: _risk_tag_counts(db, open_ids),
        default={"conversation_ids": [], "by_code": []},
    )

    pending_reply = 0
    waiting_queue = 0
    ai_active = 0
    human_active = 0
    by_status: dict[str, int] = {}
    for item in open_conversations:
        by_status[item.status] = by_status.get(item.status, 0) + 1
        if item.assigned_user_id is None and item.status != "AGUARDANDO_CLIENTE":
            waiting_queue += 1
        if item.unread_count and item.unread_count > 0:
            pending_reply += 1
        state = ai_states.get(item.id)
        if state in _AI_OWNED:
            ai_active += 1
        elif item.assigned_user_id and not _is_ai_user(ai_user_ids, item.assigned_user_id):
            human_active += 1

    stalled_without_sale = len(
        {item["conversation_id"] for item in stuck_sales}
        | set(silent_closings)
        | set(risk_tags["conversation_ids"])
    )

    sellers = _safe_call(
        "sellers",
        lambda: _seller_rows(db, user, online, ai_user_ids, open_conversations, queue_ids),
        default=[],
    )
    queue_rows = _safe_call(
        "queues",
        lambda: _queue_rows(db, queues, open_conversations, ai_states, online, ai_user_ids),
        default=[],
    )
    funnel = _safe_call(
        "funnel",
        lambda: _today_funnel(db, day_start, now, filter_queue_ids, won_today),
        default={
            "attended": 0,
            "closing_started": 0,
            "contract_sent": 0,
            "won": won_today,
            "credit_rejected": 0,
            "opt_out": 0,
        },
    )
    alerts = _build_alerts(
        failed_jobs=failed_jobs,
        stuck_sales=stuck_sales,
        silent_count=len(silent_closings),
        pending_reply=pending_reply,
        waiting_queue=waiting_queue,
        risk_count=len(risk_tags["conversation_ids"]),
        recent_ops=_safe_call("ops_alerts", lambda: _recent_ops_alerts(db), default=[]),
    )
    attention = _safe_call(
        "attention",
        lambda: _attention_list(
            db,
            open_conversations,
            stuck_sales=stuck_sales,
            silent_closings=silent_closings,
            failed_jobs=failed_jobs,
            risk_ids=set(risk_tags["conversation_ids"]),
            ai_states=ai_states,
        ),
        default=[],
    )

    return {
        "generated_at": now.isoformat(),
        "day_start": day_start.isoformat(),
        "refresh_seconds": 20,
        "kpis": {
            "open": len(open_conversations),
            "started_today": started_today,
            "pending_reply": pending_reply,
            "waiting_queue": waiting_queue,
            "ai_active": ai_active,
            "human_active": human_active,
            "won_today": won_today,
            "stalled_without_sale": stalled_without_sale,
            "failed_ai": len(failed_jobs),
            "sellers_online": sum(1 for row in sellers if row["online"] and not row["is_ai"]),
            "sellers_total": sum(1 for row in sellers if not row["is_ai"]),
        },
        "by_status": [
            {"code": code, "label": _STATUS_LABELS.get(code, code), "count": count}
            for code, count in sorted(by_status.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "funnel_today": funnel,
        "queues": queue_rows,
        "sellers": sellers,
        "alerts": alerts,
        "attention": attention[:40],
        "risk_tags": risk_tags["by_code"],
        "filters": {
            "queue_id": queue_id,
            "queues": [{"id": item.id, "name": item.name} for item in all_queues],
        },
    }


def _safe_call(label: str, fn, *, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - painel gerencial não pode cair por um bloco
        LOG.exception("ops_wallboard_block_failed block=%s", label)
        return default


def _empty_payload(now, day_start) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "day_start": day_start.isoformat(),
        "refresh_seconds": 20,
        "kpis": {
            "open": 0,
            "started_today": 0,
            "pending_reply": 0,
            "waiting_queue": 0,
            "ai_active": 0,
            "human_active": 0,
            "won_today": 0,
            "stalled_without_sale": 0,
            "failed_ai": 0,
            "sellers_online": 0,
            "sellers_total": 0,
        },
        "by_status": [],
        "funnel_today": {
            "attended": 0,
            "closing_started": 0,
            "contract_sent": 0,
            "won": 0,
            "credit_rejected": 0,
            "opt_out": 0,
        },
        "queues": [],
        "sellers": [],
        "alerts": [],
        "attention": [],
        "risk_tags": [],
        "filters": {"queue_id": None, "queues": []},
    }


def _count_opened_today(day_start, queue_ids: list[int] | None):
    stmt = select(func.count(Conversation.id)).where(Conversation.opened_at >= day_start)
    if queue_ids is not None:
        stmt = stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Conversation.id,
        ).where(ConversationQueueLink.queue_id.in_(queue_ids or [-1]))
    return stmt


def _today_funnel(
    db: Session,
    day_start,
    now,
    queue_ids: list[int] | None,
    won_today: int,
) -> dict[str, int]:
    attended_stmt = (
        select(Message.conversation_id)
        .join(
            AIConversationState,
            AIConversationState.conversation_id == Message.conversation_id,
        )
        .where(
            Message.direction == "INBOUND",
            Message.created_at >= day_start,
            Message.created_at < now,
        )
        .distinct()
    )
    closings_stmt = select(AIClosingState).where(
        AIClosingState.updated_at >= day_start,
        AIClosingState.updated_at < now,
    )
    opt_out_stmt = select(AIConversationState.id).where(
        AIConversationState.state == "OPT_OUT",
        AIConversationState.updated_at >= day_start,
        AIConversationState.updated_at < now,
    )
    if queue_ids is not None:
        queue_filter = queue_ids or [-1]
        attended_stmt = attended_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == Message.conversation_id,
        ).where(ConversationQueueLink.queue_id.in_(queue_filter))
        closings_stmt = closings_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == AIClosingState.conversation_id,
        ).where(ConversationQueueLink.queue_id.in_(queue_filter))
        opt_out_stmt = opt_out_stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == AIConversationState.conversation_id,
        ).where(ConversationQueueLink.queue_id.in_(queue_filter))

    attended = set(db.scalars(attended_stmt))
    closings = list(db.scalars(closings_stmt))
    started = sum(
        1
        for item in closings
        if item.created_at and _naive(item.created_at) >= day_start
    )
    credit_rejected = sum(1 for item in closings if item.step == "CREDIT_REJECTED")
    contract_sent = sum(1 for item in closings if str(item.sign_url or "").strip())
    opt_out = len(list(db.scalars(opt_out_stmt)))
    return {
        "attended": len(attended),
        "closing_started": started,
        "contract_sent": contract_sent,
        "won": won_today,
        "credit_rejected": credit_rejected,
        "opt_out": opt_out,
    }


def _failed_ai_jobs(db: Session, now, queue_ids: list[int] | None) -> list[dict[str, Any]]:
    stmt = (
        select(AIJob)
        .where(AIJob.status == "FALHOU", AIJob.updated_at >= now - timedelta(hours=24))
        .order_by(AIJob.updated_at.desc())
        .limit(80)
    )
    if queue_ids is not None:
        stmt = stmt.where(AIJob.queue_id.in_(queue_ids or [-1]))
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for job in db.scalars(stmt):
        if job.conversation_id in seen:
            continue
        conv = db.get(Conversation, job.conversation_id)
        if not conv or conv.status in _CLOSED:
            continue
        response = db.scalar(
            select(Message.id).where(
                Message.conversation_id == job.conversation_id,
                Message.direction == "OUTBOUND",
                Message.id > job.message_id,
            ).limit(1)
        )
        if response:
            continue
        seen.add(job.conversation_id)
        rows.append(
            {
                "conversation_id": job.conversation_id,
                "queue_id": job.queue_id,
                "error": (job.error or "falha no processamento")[:160],
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
        )
        if len(rows) >= 25:
            break
    return rows


def _stuck_sales(db: Session, now, closing_states: list[AIClosingState]) -> list[dict[str, Any]]:
    candidates = []
    for state in closing_states:
        error = (state.last_error or "").strip()
        if state.step in _SALE_DONE or not (error or state.step == "WAITING_CRM_READY"):
            continue
        candidates.append(state)
    if not candidates:
        return []

    candidate_ids = [state.conversation_id for state in candidates]
    recent_ids = {
        cid
        for (cid,) in db.execute(
            select(Message.conversation_id)
            .where(
                Message.conversation_id.in_(candidate_ids),
                Message.created_at >= now - STUCK_SALE_AFTER,
            )
            .distinct()
        )
    }

    rows: list[dict[str, Any]] = []
    for state in candidates:
        if state.conversation_id in recent_ids:
            continue
        collected = _parse_collected(state.collected_json)
        error = (state.last_error or "").strip()
        rows.append(
            {
                "conversation_id": state.conversation_id,
                "step": state.step,
                "step_label": _STEP_LABELS.get(state.step, state.step),
                "error": error[:160],
                "plan": state.plan_label or "",
                "name": str(collected.get("name") or "").strip(),
            }
        )
    return rows


def _silent_closing_ids(db: Session, closing_states: list[AIClosingState]) -> set[int]:
    from app.services.ai_followups import (  # noqa: PLC0415
        MAX_UNANSWERED_CLOSING_FOLLOWUPS,
        closing_awaits_customer,
    )
    from app.models import AIFollowupJob  # noqa: PLC0415

    silent: set[int] = set()
    # Limita o N+1: só avalia os primeiros candidatos elegíveis.
    checked = 0
    for state in closing_states:
        if state.step in _SALE_DONE:
            continue
        if checked >= 80:
            break
        checked += 1
        if not closing_awaits_customer(db, state.conversation_id):
            continue
        reminder_count = int(
            db.scalar(
                select(func.count(AIFollowupJob.id)).where(
                    AIFollowupJob.conversation_id == state.conversation_id,
                    AIFollowupJob.sent_at.is_not(None),
                )
            )
            or 0
        )
        if reminder_count < MAX_UNANSWERED_CLOSING_FOLLOWUPS:
            continue
        silent.add(state.conversation_id)
    return silent


def _risk_tag_counts(db: Session, open_ids: list[int]) -> dict[str, Any]:
    if not open_ids:
        return {"conversation_ids": [], "by_code": []}
    tags = list(
        db.scalars(
            select(ConversationCommercialTag).where(
                ConversationCommercialTag.conversation_id.in_(open_ids),
                ConversationCommercialTag.code.in_(sorted(_RISK_TAGS)),
            )
        )
    )
    by_code: dict[str, int] = {}
    ids: set[int] = set()
    for tag in tags:
        by_code[tag.code] = by_code.get(tag.code, 0) + 1
        ids.add(tag.conversation_id)
    return {
        "conversation_ids": sorted(ids),
        "by_code": [{"code": code, "count": count} for code, count in sorted(by_code.items())],
    }


def _seller_rows(
    db: Session,
    user: User,
    online: set[int],
    ai_user_ids: set[int],
    open_conversations: list[Conversation],
    queue_ids: list[int],
) -> list[dict[str, Any]]:
    if user.role not in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        return []
    stmt = select(User).where(User.active.is_(True), User.role == "CONSULTOR").order_by(User.name)
    people = list(db.scalars(stmt))
    if user.role == "SUPERVISOR":
        if user.branch_id:
            people = [item for item in people if item.branch_id == user.branch_id]
        else:
            people = [item for item in people if item.team == user.team]
    # Inclui perfis de IA ativos (não fazem login, mas aparecem na frota).
    ai_people = list(
        db.scalars(
            select(User).where(User.id.in_(sorted(ai_user_ids) or [-1]), User.active.is_(True))
        )
    )
    people = people + [item for item in ai_people if item.id not in {p.id for p in people}]
    if queue_ids:
        member_ids = set(
            db.scalars(
                select(ServiceQueueMember.user_id).where(
                    ServiceQueueMember.queue_id.in_(queue_ids),
                    ServiceQueueMember.active.is_(True),
                )
            )
        )
        people = [item for item in people if item.id in member_ids or item.id in ai_user_ids]

    open_counts: dict[int, int] = {}
    pending_counts: dict[int, int] = {}
    for item in open_conversations:
        if not item.assigned_user_id:
            continue
        open_counts[item.assigned_user_id] = open_counts.get(item.assigned_user_id, 0) + 1
        if item.unread_count and item.unread_count > 0:
            pending_counts[item.assigned_user_id] = pending_counts.get(item.assigned_user_id, 0) + 1

    presence_map = {
        row.user_id: row
        for row in db.scalars(select(WhatsAppPresence).where(WhatsAppPresence.user_id.in_([p.id for p in people] or [-1])))
    }

    rows: list[dict[str, Any]] = []
    for person in people:
        is_ai = person.id in ai_user_ids
        presence = presence_map.get(person.id)
        rows.append(
            {
                "id": person.id,
                "name": person.name,
                "role": person.role,
                "is_ai": is_ai,
                "online": person.id in online,
                "open": open_counts.get(person.id, 0),
                "pending": pending_counts.get(person.id, 0),
                "last_seen_at": (
                    presence.last_heartbeat_at.isoformat()
                    if presence and presence.last_heartbeat_at
                    else None
                ),
            }
        )
    rows.sort(key=lambda row: (-int(row["online"]), -row["pending"], -row["open"], row["name"]))
    return rows


def _queue_rows(
    db: Session,
    queues: list[ServiceQueue],
    open_conversations: list[Conversation],
    ai_states: dict[int, str],
    online: set[int],
    ai_user_ids: set[int],
) -> list[dict[str, Any]]:
    links: dict[int, list[int]] = {}
    if open_conversations:
        for queue_id, conversation_id in db.execute(
            select(ConversationQueueLink.queue_id, ConversationQueueLink.conversation_id).where(
                ConversationQueueLink.conversation_id.in_([item.id for item in open_conversations])
            )
        ):
            links.setdefault(queue_id, []).append(conversation_id)

    open_by_id = {item.id: item for item in open_conversations}
    rows: list[dict[str, Any]] = []
    for queue in queues:
        member_ids = set(
            db.scalars(
                select(ServiceQueueMember.user_id).where(
                    ServiceQueueMember.queue_id == queue.id,
                    ServiceQueueMember.active.is_(True),
                )
            )
        )
        human_members = [uid for uid in member_ids if uid not in ai_user_ids]
        conv_ids = links.get(queue.id, [])
        open_count = len(conv_ids)
        waiting = 0
        pending = 0
        ai_count = 0
        for cid in conv_ids:
            item = open_by_id.get(cid)
            if not item:
                continue
            if item.assigned_user_id is None and item.status != "AGUARDANDO_CLIENTE":
                waiting += 1
            if item.unread_count and item.unread_count > 0:
                pending += 1
            if ai_states.get(cid) in _AI_OWNED:
                ai_count += 1
        rows.append(
            {
                "id": queue.id,
                "name": queue.name,
                "code": queue.code or "",
                "open": open_count,
                "waiting": waiting,
                "pending": pending,
                "ai_active": ai_count,
                "members": len(human_members),
                "online": sum(1 for uid in human_members if uid in online),
            }
        )
    rows.sort(key=lambda row: (-row["pending"], -row["waiting"], -row["open"], row["name"]))
    return rows


def _recent_ops_alerts(db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.action == ALERT_ACTION, AuditLog.created_at >= utcnow() - timedelta(hours=24))
            .order_by(AuditLog.created_at.desc())
            .limit(20)
        )
    )
    return [
        {
            "key": row.entity_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _build_alerts(
    *,
    failed_jobs: list[dict],
    stuck_sales: list[dict],
    silent_count: int,
    pending_reply: int,
    waiting_queue: int,
    risk_count: int,
    recent_ops: list[dict],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if failed_jobs:
        alerts.append(
            {
                "severity": "CRITICAL",
                "code": "failed_ai",
                "title": "Falhas de atendimento IA",
                "detail": f"{len(failed_jobs)} conversa(s) com job da IA falhou e ainda sem resposta ao cliente.",
                "count": len(failed_jobs),
            }
        )
    if stuck_sales:
        alerts.append(
            {
                "severity": "CRITICAL",
                "code": "stuck_sale",
                "title": "Vendas paradas no fechamento",
                "detail": f"{len(stuck_sales)} venda(s) com erro ou aguardando CRM sem avanço recente.",
                "count": len(stuck_sales),
            }
        )
    if silent_count:
        alerts.append(
            {
                "severity": "WARNING",
                "code": "silent_closing",
                "title": "Fechamento em silêncio",
                "detail": f"{silent_count} cliente(s) pararam de responder no fechamento após lembretes.",
                "count": silent_count,
            }
        )
    if pending_reply >= 10:
        alerts.append(
            {
                "severity": "WARNING" if pending_reply < 25 else "CRITICAL",
                "code": "pending_reply",
                "title": "Clientes aguardando resposta",
                "detail": f"{pending_reply} conversa(s) abertas com mensagem não lida/respondida.",
                "count": pending_reply,
            }
        )
    if waiting_queue >= 8:
        alerts.append(
            {
                "severity": "WARNING",
                "code": "waiting_queue",
                "title": "Fila sem responsável",
                "detail": f"{waiting_queue} conversa(s) abertas sem vendedor atribuído.",
                "count": waiting_queue,
            }
        )
    if risk_count:
        alerts.append(
            {
                "severity": "INFO",
                "code": "risk_tags",
                "title": "Risco comercial detectado",
                "detail": f"{risk_count} conversa(s) com tag de risco/documentos/assinatura pendente.",
                "count": risk_count,
            }
        )
    if recent_ops and not alerts:
        alerts.append(
            {
                "severity": "INFO",
                "code": "ops_feed",
                "title": "Alertas operacionais recentes",
                "detail": f"{len(recent_ops)} aviso(s) enviados nas últimas 24h (também no WhatsApp do responsável).",
                "count": len(recent_ops),
            }
        )
    return alerts


def _attention_list(
    db: Session,
    open_conversations: list[Conversation],
    *,
    stuck_sales: list[dict],
    silent_closings: set[int],
    failed_jobs: list[dict],
    risk_ids: set[int],
    ai_states: dict[int, str],
) -> list[dict[str, Any]]:
    reasons: dict[int, list[str]] = {}
    for item in stuck_sales:
        reasons.setdefault(item["conversation_id"], []).append(
            f"Venda parada ({item.get('step_label') or item.get('step')})"
        )
    for cid in silent_closings:
        reasons.setdefault(cid, []).append("Cliente silencioso no fechamento")
    for item in failed_jobs:
        reasons.setdefault(item["conversation_id"], []).append("Falha da IA sem resposta")
    for cid in risk_ids:
        reasons.setdefault(cid, []).append("Tag de risco comercial")

    open_by_id = {item.id: item for item in open_conversations}
    # Also include stuck/failed that may still be open
    for cid in list(reasons):
        if cid not in open_by_id:
            conv = db.get(Conversation, cid)
            if conv and conv.status in OPEN_CONVERSATION_STATUSES:
                open_by_id[cid] = conv

    rows: list[dict[str, Any]] = []
    for cid, why in reasons.items():
        item = open_by_id.get(cid)
        if not item:
            continue
        contact = item.contact if getattr(item, "contact", None) else db.get(Contact, item.contact_id) if item.contact_id else None
        rows.append(
            {
                "conversation_id": cid,
                "contact_name": (contact.name if contact else "") or "Cliente",
                "phone": (contact.phone if contact else "") or "",
                "status": item.status,
                "status_label": _STATUS_LABELS.get(item.status, item.status),
                "owner": item.assigned_user.name if item.assigned_user else "Sem responsável",
                "ai_state": ai_states.get(cid) or "",
                "unread": int(item.unread_count or 0),
                "reasons": why,
                "href": f"/conversations/{cid}",
            }
        )
    # Also surface high-unread without other reasons
    for item in open_conversations:
        if item.id in reasons:
            continue
        if (item.unread_count or 0) < 3 and item.assigned_user_id is not None:
            continue
        if (item.unread_count or 0) == 0 and item.assigned_user_id is not None:
            continue
        if item.assigned_user_id is None or (item.unread_count or 0) >= 3:
            contact = item.contact if getattr(item, "contact", None) else None
            why = []
            if item.assigned_user_id is None:
                why.append("Sem responsável")
            if (item.unread_count or 0) >= 3:
                why.append(f"{item.unread_count} não lidas")
            rows.append(
                {
                    "conversation_id": item.id,
                    "contact_name": (contact.name if contact else "") or "Cliente",
                    "phone": (contact.phone if contact else "") or "",
                    "status": item.status,
                    "status_label": _STATUS_LABELS.get(item.status, item.status),
                    "owner": item.assigned_user.name if item.assigned_user else "Sem responsável",
                    "ai_state": ai_states.get(item.id) or "",
                    "unread": int(item.unread_count or 0),
                    "reasons": why,
                    "href": f"/conversations/{item.id}",
                }
            )
    rows.sort(key=lambda row: (-len(row["reasons"]), -row["unread"], row["conversation_id"]))
    return rows


def resolve_won_today(db: Session, day_start, queue_ids: list[int] | None = None) -> int:
    """Contagem precisa de vendas assinadas hoje (parsing de collected_json)."""
    stmt = select(AIClosingState).where(
        AIClosingState.step == "COMPLETED",
        AIClosingState.completed_at.is_not(None),
        AIClosingState.completed_at >= day_start,
    )
    if queue_ids is not None:
        stmt = stmt.join(
            ConversationQueueLink,
            ConversationQueueLink.conversation_id == AIClosingState.conversation_id,
        ).where(ConversationQueueLink.queue_id.in_(queue_ids or [-1]))
    return sum(1 for state in db.scalars(stmt) if _won_today(state, day_start))
