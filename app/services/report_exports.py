from __future__ import annotations

import io
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.chat_reports import format_duration
from app.timeutils import utc_to_local
from app.web import phone_br


ORANGE = colors.HexColor("#F36F21")
DARK = colors.HexColor("#20262D")
MUTED = colors.HexColor("#6F7881")
LIGHT = colors.HexColor("#F4F6F8")
BORDER = colors.HexColor("#DFE4E8")


def _pdf_safe(value: object) -> str:
    return str(value if value is not None else "").encode("cp1252", "replace").decode("cp1252")


def _html(value: object) -> str:
    return escape(_pdf_safe(value))


def _dt(value) -> str:
    local = utc_to_local(value)
    return local.strftime("%d/%m/%Y %H:%M") if local else "—"


def _styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "SulnetTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=DARK,
            spaceAfter=4 * mm,
        ),
        "subtitle": ParagraphStyle(
            "SulnetSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=MUTED,
            spaceAfter=4 * mm,
        ),
        "section": ParagraphStyle(
            "SulnetSection",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=DARK,
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "small": ParagraphStyle(
            "SulnetSmall",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=10,
            textColor=DARK,
        ),
        "message": ParagraphStyle(
            "SulnetMessage",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11.5,
            textColor=DARK,
        ),
        "message_meta": ParagraphStyle(
            "SulnetMessageMeta",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=MUTED,
        ),
        "center": ParagraphStyle(
            "SulnetCenter",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            alignment=TA_CENTER,
            textColor=DARK,
        ),
    }


def _page_header_footer(canvas, doc):
    canvas.saveState()
    width, height = doc.pagesize
    canvas.setFillColor(ORANGE)
    canvas.rect(0, height - 8 * mm, width, 8 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(12 * mm, height - 5.2 * mm, "SULNET PROSPECTA · WHATSAPP")
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(width - 12 * mm, 8 * mm, f"Página {doc.page}")
    canvas.restoreState()


def build_management_report_pdf(snapshot: dict[str, Any], period_label: str) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=15 * mm,
        bottomMargin=14 * mm,
        title="Relatório WhatsApp - Sulnet Prospecta",
        author="Sulnet Prospecta",
    )
    styles = _styles()
    story = [
        Paragraph("Relatório gerencial · WhatsApp", styles["title"]),
        Paragraph(
            f"Período: {_html(period_label)} · Dados respeitam o escopo de acesso do usuário que exportou.",
            styles["subtitle"],
        ),
    ]

    kpis = snapshot["kpis"]
    cards = [
        ("Conversas", str(kpis["conversations"])),
        ("Mensagens", str(kpis["messages"])),
        ("Entrada", str(kpis["inbound"])),
        ("Saída", str(kpis["outbound"])),
        ("1ª resposta média", format_duration(kpis["avg_first_response_seconds"])),
        ("Resposta média", format_duration(kpis["avg_reply_seconds"])),
        ("Atendimento médio", format_duration(kpis["avg_close_seconds"])),
        ("Leitura", f'{kpis["read_rate"]:.1f}%'),
    ]
    kpi_table = Table(
        [[Paragraph(_html(label), styles["center"]) for label, _ in cards],
         [Paragraph(_html(value), styles["center"]) for _, value in cards]],
        colWidths=[33 * mm] * len(cards),
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
        ("TEXTCOLOR", (0, 0), (-1, -1), DARK),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([kpi_table, Spacer(1, 4 * mm)])

    story.append(Paragraph("Performance por operador", styles["section"]))
    operator_rows = [["Operador", "Conversas", "Mensagens", "1ª resposta", "Resposta média"]]
    for item in snapshot["operators"][:30]:
        operator_rows.append([
            item["name"],
            str(item["conversations"]),
            str(item["outbound_messages"]),
            format_duration(item["avg_first_response_seconds"]),
            format_duration(item["avg_reply_seconds"]),
        ])
    if len(operator_rows) == 1:
        operator_rows.append(["Sem dados no período", "—", "—", "—", "—"])
    operator_table = Table(
        [[Paragraph(_html(value), styles["small"]) for value in row] for row in operator_rows],
        colWidths=[66 * mm, 26 * mm, 28 * mm, 38 * mm, 38 * mm],
        repeatRows=1,
    )
    operator_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([operator_table, Spacer(1, 4 * mm)])

    story.append(Paragraph("Conversas no período", styles["section"]))
    conversation_rows = [["Contato", "Telefone", "Responsável", "Status", "Msgs", "1ª resposta", "Resp. média", "Aberta em"]]
    for item in snapshot["conversations"][:120]:
        conversation_rows.append([
            item["name"],
            phone_br(item["phone"]),
            item["owner"],
            item["status"].replace("_", " "),
            str(item["message_count"]),
            format_duration(item["first_response_seconds"]),
            format_duration(item["avg_reply_seconds"]),
            _dt(item["opened_at"]),
        ])
    if len(conversation_rows) == 1:
        conversation_rows.append(["Sem conversas", "—", "—", "—", "—", "—", "—", "—"])
    conversation_table = Table(
        [[Paragraph(_html(value), styles["small"]) for value in row] for row in conversation_rows],
        colWidths=[45 * mm, 29 * mm, 42 * mm, 28 * mm, 15 * mm, 28 * mm, 28 * mm, 31 * mm],
        repeatRows=1,
    )
    conversation_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ORANGE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    story.append(conversation_table)

    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    return buffer.getvalue()


def build_conversation_pdf(conversation, metrics: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=14 * mm,
        title=f"Conversa WhatsApp {conversation.id}",
        author="Sulnet Prospecta",
    )
    styles = _styles()
    contact_name = (
        conversation.contact.business_name
        or conversation.contact.name
        or conversation.contact.phone
    )
    story = [
        Paragraph(f"Extrato de conversa · #{conversation.id}", styles["title"]),
        Paragraph(
            f"<b>Contato:</b> {_html(contact_name)} &nbsp;&nbsp; "
            f"<b>Telefone:</b> {_html(phone_br(conversation.contact.phone))}<br/>"
            f"<b>Responsável:</b> {_html(conversation.assigned_user.name if conversation.assigned_user else 'Fila sem responsável')} &nbsp;&nbsp; "
            f"<b>Status:</b> {_html(conversation.status.replace('_', ' '))}<br/>"
            f"<b>Aberta:</b> {_dt(conversation.opened_at)} &nbsp;&nbsp; "
            f"<b>Encerrada:</b> {_dt(conversation.closed_at)}",
            styles["subtitle"],
        ),
    ]

    metric_data = [
        ["Mensagens", str(metrics.get("message_count", 0))],
        ["Primeira resposta", format_duration(metrics.get("first_response_seconds"))],
        ["Resposta média", format_duration(metrics.get("avg_reply_seconds"))],
        ["Tempo até encerramento", format_duration(metrics.get("close_seconds"))],
    ]
    metric_table = Table(
        [[Paragraph(_html(a), styles["small"]), Paragraph(_html(b), styles["small"])] for a, b in metric_data],
        colWidths=[50 * mm, 50 * mm],
    )
    metric_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([metric_table, Spacer(1, 5 * mm), Paragraph("Histórico completo", styles["section"])])

    for message in conversation.messages:
        if message.direction == "INBOUND":
            who = "Cliente"
        elif message.direction == "INTERNAL":
            who = f"Nota interna · {message.sender_user.name if message.sender_user else 'Sistema'}"
        else:
            who = message.sender_user.name if message.sender_user else "Sulnet"
        media = ""
        if message.media_url:
            media = f"<br/><font color='#6F7881'>Anexo: {_html(message.message_type)} · {_html(message.mime_type or 'arquivo')}</font>"
        body = _html(message.body or f"[{message.message_type}]").replace("\n", "<br/>")
        card = Table(
            [[
                Paragraph(
                    f"{_html(who)} · {_dt(message.created_at)} · {_html(message.status)}",
                    styles["message_meta"],
                )
            ], [
                Paragraph(body + media, styles["message"])
            ]],
            colWidths=[165 * mm],
        )
        bg = colors.HexColor("#FFF3EB") if message.direction == "OUTBOUND" else (
            colors.HexColor("#F4F6F8") if message.direction == "INTERNAL" else colors.white
        )
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([KeepTogether(card), Spacer(1, 2.2 * mm)])

    if not conversation.messages:
        story.append(Paragraph("Nenhuma mensagem registrada.", styles["small"]))

    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    return buffer.getvalue()
