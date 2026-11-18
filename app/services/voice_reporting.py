"""Painel operacional de voz, com métricas reais do banco e trilha de comandos.

Somente as rotas ADMIN do Prospecta VOZ podem divulgar estes resultados.
Nenhuma consulta deste módulo origina chamadas ou altera destinatários.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, VoiceCallEvent, VoiceCampaign, VoiceRecipient
from app.services.voice_runtime import within_call_window
from app.services.voice_test import aware


ACTIVE_CALL_STATES = ("INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO")
FAILED_CALL_STATES = (
    "FALHOU", "FALHA_TRANSFERENCIA", "TIMEOUT", "SEM_ATENDIMENTO",
    "SEM_RESPOSTA", "OCUPADO", "INTERROMPIDA", "ERRO",
)
COMMAND_LABELS = {
    "VOICE_CAMPAIGN_START_BLOCKED": "Tentativa de início bloqueada",
    "VOICE_CAMPAIGN_STARTED": "Início autorizado pelo administrador",
    "VOICE_CAMPAIGN_PAUSED": "Campanha pausada pelo administrador",
    "VOICE_LEGAL_BASIS_CONFIRMED": "Base legal confirmada pelo administrador",
}


def _iso(value: datetime | None) -> str | None:
    date = aware(value)
    return date.astimezone(timezone.utc).isoformat() if date else None


def _last_command(db: Session, campaign_id: int) -> dict:
    row = db.scalar(select(AuditLog).where(
        AuditLog.entity_type == "voice_campaign",
        AuditLog.entity_id == str(campaign_id),
        AuditLog.action.in_(tuple(COMMAND_LABELS)),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return {
            "label": "Nenhuma solicitação de início registrada",
            "at": None, "detail": "A campanha ainda não recebeu uma ordem de discagem.",
        }
    details = ""
    if row.action == "VOICE_CAMPAIGN_START_BLOCKED":
        try:
            reasons = json.loads(row.payload_json or "{}").get("reasons") or []
            details = " / ".join(str(reason) for reason in reasons[:4])
        except (ValueError, TypeError):
            details = "Consulte os pré-requisitos da campanha."
    return {
        "label": COMMAND_LABELS[row.action],
        "at": _iso(row.created_at),
        "detail": details,
    }


def campaign_operational_report(db: Session, campaign: VoiceCampaign,
                                readiness: list[str] | None = None) -> dict:
    """Usa somente registros persistidos; nunca inventa chamada realizada."""
    reasons = readiness if readiness is not None else []
    summary = db.execute(select(
        func.count(VoiceRecipient.id),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status == "PENDENTE"),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.attempts > 0),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status.in_(ACTIVE_CALL_STATES)),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.answered_at.is_not(None)),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.transferred_at.is_not(None)),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status.in_(FAILED_CALL_STATES)),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status == "BLOQUEADA"),
    ).where(VoiceRecipient.campaign_id == campaign.id)).one()
    stats = dict(zip(
        ("total", "pending", "attempted", "in_progress", "answered",
         "transferred", "failed", "blocked"), map(int, summary), strict=True,
    ))

    attempts = list(db.scalars(select(VoiceRecipient).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.attempts > 0,
    ).order_by(VoiceRecipient.last_attempt_at.desc(), VoiceRecipient.id.desc()).limit(8)))
    recent_attempts = [{
        "name": row.name or "Contato",
        "phone_suffix": (row.phone or "")[-4:].rjust(4, "*"),
        "status": row.status,
        "at": _iso(row.last_attempt_at),
        "detail": row.last_error or row.last_event or "",
    } for row in attempts]

    event_rows = db.execute(select(VoiceCallEvent, VoiceRecipient).join(
        VoiceRecipient, VoiceCallEvent.recipient_id == VoiceRecipient.id,
    ).where(VoiceRecipient.campaign_id == campaign.id)
      .order_by(VoiceCallEvent.id.desc()).limit(10)).all()
    recent_events = [{
        "event": event.event,
        "contact": recipient.name or "Contato",
        "phone_suffix": (recipient.phone or "")[-4:].rjust(4, "*"),
        "detail": event.detail,
        "at": _iso(event.created_at),
    } for event, recipient in event_rows]

    status = campaign.status
    if status == "EM_EXECUCAO":
        headline = ("Em execução — aguardando janela de discagem"
                    if not within_call_window(campaign)
                    else "Em execução — monitoramento ativo")
    elif status == "PAUSADA":
        headline = "Pausada — nenhuma nova ligação será iniciada"
    elif status == "CONCLUIDA":
        headline = "Concluída — processamento finalizado"
    elif status in {"RASCUNHO", "PRONTA", "EM_HOMOLOGACAO"}:
        headline = (f"Não iniciada — {len(reasons)} pendência(s)"
                    if reasons else "Pronta para iniciar — nenhuma chamada realizada")
    else:
        headline = f"Situação atual: {status.replace('_', ' ')}"

    return {
        "id": campaign.id,
        "status": status,
        "status_label": {
            "RASCUNHO": "RASCUNHO", "PRONTA": "PRONTA",
            "EM_HOMOLOGACAO": "PREPARADA", "EM_EXECUCAO": "EM EXECUÇÃO",
            "PAUSADA": "PAUSADA", "CONCLUIDA": "CONCLUÍDA",
        }.get(status, status.replace("_", " ")),
        "headline": headline,
        "readiness": list(reasons),
        "stats": stats,
        "last_command": _last_command(db, campaign.id),
        "started_at": _iso(campaign.started_at),
        "last_call_at": _iso(campaign.last_call_at),
        "last_attempts": recent_attempts,
        "events": recent_events,
    }
