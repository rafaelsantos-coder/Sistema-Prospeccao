from __future__ import annotations

import csv
import io
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse, Response

from app.database import get_db
from app.models import (
    Branch,
    Conversation,
    ConversationAssignment,
    Message,
    User,
)
from app.security import get_current_user
from app.services.audit import write_audit
from app.services.chat_reports import (
    ChatReportFilters,
    build_chat_report,
    conversation_metrics,
    format_duration,
)
from app.services.conversation_engine import apply_conversation_scope
from app.services.report_exports import (
    build_conversation_pdf,
    build_management_report_pdf,
)
from app.timeutils import utc_to_local
from app.web import phone_br, render


router = APIRouter()


def _int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _filters_from_request(request: Request) -> ChatReportFilters:
    defaults = ChatReportFilters.defaults()
    params = request.query_params
    return ChatReportFilters(
        date_from=(params.get("date_from") or defaults.date_from).strip(),
        date_to=(params.get("date_to") or defaults.date_to).strip(),
        q=(params.get("q") or "").strip(),
        phone=(params.get("phone") or "").strip(),
        operator_id=_int_or_none(params.get("operator_id")),
        branch_id=_int_or_none(params.get("branch_id")),
        team=(params.get("team") or "").strip().upper(),
        status=(params.get("status") or "").strip().upper(),
        direction=(params.get("direction") or "").strip().upper(),
        message_type=(params.get("message_type") or "").strip().upper(),
        channel_phone=(params.get("channel_phone") or "").strip(),
    )


def _available_operators(db: Session, user: User) -> list[User]:
    if user.role == "CONSULTOR":
        return [user]
    stmt = select(User).where(User.active.is_(True)).order_by(User.name)
    if user.role == "SUPERVISOR":
        if user.branch_id:
            stmt = stmt.where(User.branch_id == user.branch_id)
        else:
            stmt = stmt.where(User.team == user.team)
    return list(db.scalars(stmt))


def _available_branches(db: Session, user: User) -> list[Branch]:
    stmt = select(Branch).where(Branch.active.is_(True)).order_by(Branch.city)
    if user.role == "SUPERVISOR" and user.branch_id:
        stmt = stmt.where(Branch.id == user.branch_id)
    if user.role == "CONSULTOR" and user.branch_id:
        stmt = stmt.where(Branch.id == user.branch_id)
    return list(db.scalars(stmt))


def _conversation_for_user(db: Session, user: User, conversation_id: int) -> Conversation | None:
    stmt = apply_conversation_scope(
        select(Conversation).options(
            selectinload(Conversation.contact),
            selectinload(Conversation.assigned_user),
            selectinload(Conversation.branch),
            selectinload(Conversation.messages).selectinload(Message.sender_user),
            selectinload(Conversation.assignments).selectinload(ConversationAssignment.user),
        ),
        user,
    ).where(Conversation.id == conversation_id)
    return db.scalar(stmt)


def _dt(value) -> str:
    local = utc_to_local(value)
    return local.strftime("%d/%m/%Y %H:%M:%S") if local else ""


def _csv_response(rows: list[list[str]], filename: str) -> Response:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerows(rows)
    payload = stream.getvalue().encode("utf-8-sig")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _period_label(filters: ChatReportFilters) -> str:
    return f"{filters.date_from} a {filters.date_to}"


@router.get("/reports/whatsapp")
def whatsapp_reports(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    filters = _filters_from_request(request)
    snapshot = build_chat_report(db, user, filters)
    query_string = urlencode(filters.as_query_dict())

    return render(
        request,
        "reports/whatsapp.html",
        user=user,
        active_nav="reports",
        page_title="Relatórios WhatsApp",
        report=snapshot,
        filters=filters,
        operators=_available_operators(db, user),
        branches=_available_branches(db, user),
        query_string=query_string,
        format_duration=format_duration,
    )


@router.get("/reports/whatsapp/export/conversations.csv")
def export_conversations_csv(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    filters = _filters_from_request(request)
    report = build_chat_report(db, user, filters)

    rows = [[
        "Conversa",
        "Contato",
        "Telefone",
        "Responsável",
        "Equipe",
        "Unidade",
        "Status",
        "Aberta em",
        "Encerrada em",
        "Última mensagem",
        "Mensagens no período",
        "Entradas",
        "Saídas",
        "Primeira resposta",
        "Tempo médio de resposta",
        "Tempo até encerramento",
        "Tempo até atribuição",
        "Espera atual",
    ]]
    for item in report["conversations"]:
        rows.append([
            str(item["id"]),
            item["name"],
            phone_br(item["phone"]),
            item["owner"],
            item["team"],
            item["branch"],
            item["status"],
            _dt(item["opened_at"]),
            _dt(item["closed_at"]),
            _dt(item["last_message_at"]),
            str(item["message_count"]),
            str(item["inbound_count"]),
            str(item["outbound_count"]),
            format_duration(item["first_response_seconds"]),
            format_duration(item["avg_reply_seconds"]),
            format_duration(item["close_seconds"]),
            format_duration(item["assignment_seconds"]),
            format_duration(item["pending_wait_seconds"]),
        ])

    write_audit(
        db,
        user=user,
        action="WHATSAPP_REPORT_EXPORTED",
        entity_type="report",
        entity_id="conversations_csv",
        payload={"filters": filters.as_query_dict(), "rows": len(rows) - 1},
    )
    db.commit()
    return _csv_response(rows, "relatorio-conversas-whatsapp.csv")


@router.get("/reports/whatsapp/export/messages.csv")
def export_messages_csv(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    filters = _filters_from_request(request)
    report = build_chat_report(db, user, filters)

    conversation_map = {item.id: item for item in report["conversation_objects"]}
    rows = [[
        "Data/Hora",
        "Conversa",
        "Contato",
        "Telefone",
        "Direção",
        "Tipo",
        "Operador",
        "Status",
        "Mensagem",
        "MIME",
        "ID provedor",
        "ID Z-API",
        "Enviada em",
        "Entregue em",
        "Lida em",
    ]]
    for item in report["messages"]:
        conversation = conversation_map.get(item.conversation_id)
        if not conversation:
            continue
        sender = ""
        if item.direction == "INBOUND":
            sender = "Cliente"
        elif item.direction == "INTERNAL":
            sender = item.sender_user.name if item.sender_user else "Sistema"
        else:
            sender = item.sender_user.name if item.sender_user else "Sulnet"
        rows.append([
            _dt(item.created_at),
            str(item.conversation_id),
            (
                conversation.contact.business_name
                or conversation.contact.name
                or conversation.contact.phone
            ),
            phone_br(conversation.contact.phone),
            item.direction,
            item.message_type,
            sender,
            item.status,
            (item.body or "").replace("\r", " ").replace("\n", " "),
            item.mime_type or "",
            item.provider_message_id or "",
            item.provider_zaap_id or "",
            _dt(item.sent_at),
            _dt(item.delivered_at),
            _dt(item.read_at),
        ])

    write_audit(
        db,
        user=user,
        action="WHATSAPP_REPORT_EXPORTED",
        entity_type="report",
        entity_id="messages_csv",
        payload={"filters": filters.as_query_dict(), "rows": len(rows) - 1},
    )
    db.commit()
    return _csv_response(rows, "extrato-mensagens-whatsapp.csv")


@router.get("/reports/whatsapp/export/audit.csv")
def export_audit_csv(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    filters = _filters_from_request(request)
    report = build_chat_report(db, user, filters)

    rows = [["Data/Hora", "Usuário", "Ação", "Conversa", "Detalhes"]]
    for item in report["audit_logs"]:
        rows.append([
            _dt(item.created_at),
            item.user.name if item.user else "Sistema",
            item.action,
            item.entity_id or "",
            item.payload_json or "{}",
        ])

    write_audit(
        db,
        user=user,
        action="WHATSAPP_REPORT_EXPORTED",
        entity_type="report",
        entity_id="audit_csv",
        payload={"filters": filters.as_query_dict(), "rows": len(rows) - 1},
    )
    db.commit()
    return _csv_response(rows, "auditoria-atendimentos-whatsapp.csv")


@router.get("/reports/whatsapp/export/report.pdf")
def export_management_pdf(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    filters = _filters_from_request(request)
    report = build_chat_report(db, user, filters)
    payload = build_management_report_pdf(report, _period_label(filters))

    write_audit(
        db,
        user=user,
        action="WHATSAPP_REPORT_EXPORTED",
        entity_type="report",
        entity_id="management_pdf",
        payload={"filters": filters.as_query_dict(), "conversations": report["kpis"]["conversations"]},
    )
    db.commit()
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="relatorio-gerencial-whatsapp.pdf"'},
    )


@router.get("/reports/whatsapp/conversations/{conversation_id}.csv")
def export_conversation_csv(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conversation = _conversation_for_user(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    contact_name = (
        conversation.contact.business_name
        or conversation.contact.name
        or conversation.contact.phone
    )
    rows = [[
        "Data/Hora",
        "Direção",
        "Remetente",
        "Tipo",
        "Status",
        "Mensagem",
        "MIME",
        "ID provedor",
        "ID Z-API",
    ]]
    for item in conversation.messages:
        if item.direction == "INBOUND":
            sender = "Cliente"
        elif item.direction == "INTERNAL":
            sender = item.sender_user.name if item.sender_user else "Sistema"
        else:
            sender = item.sender_user.name if item.sender_user else "Sulnet"
        rows.append([
            _dt(item.created_at),
            item.direction,
            sender,
            item.message_type,
            item.status,
            (item.body or "").replace("\r", " ").replace("\n", " "),
            item.mime_type or "",
            item.provider_message_id or "",
            item.provider_zaap_id or "",
        ])

    write_audit(
        db,
        user=user,
        action="WHATSAPP_CONVERSATION_EXPORTED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"format": "csv", "messages": len(conversation.messages)},
    )
    db.commit()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", contact_name).strip("-")[:50] or "contato"
    return _csv_response(rows, f"conversa-{conversation.id}-{safe_name}.csv")


@router.get("/reports/whatsapp/conversations/{conversation_id}.pdf")
def export_conversation_pdf(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conversation = _conversation_for_user(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    metrics = conversation_metrics(conversation)
    payload = build_conversation_pdf(conversation, metrics)
    write_audit(
        db,
        user=user,
        action="WHATSAPP_CONVERSATION_EXPORTED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"format": "pdf", "messages": len(conversation.messages)},
    )
    db.commit()
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="conversa-{conversation.id}-whatsapp.pdf"'
        },
    )
