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
from app.services.voice_native_routing import NATIVE_ORIGIN_NUMBERS
from app.services.voice_runtime import next_call_window_start, within_call_window
from app.services.voice_test import aware


ACTIVE_CALL_STATES = ("INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO")
# Não confundir chamadas que não foram atendidas com defeitos da central.
# FALHA_AUDIO e FALHA_TRANSFERENCIA são falhas técnicas; tocar e não atender
# ou desligar são resultados de ligação, não erros de infraestrutura.
TECHNICAL_FAILURE_STATES = (
    "FALHOU", "FALHA_TRANSFERENCIA", "FALHA_AUDIO", "ERRO",
)
NO_ANSWER_STATES = ("SEM_ATENDIMENTO", "SEM_RESPOSTA")
INTERRUPTED_STATES = ("INTERROMPIDA", "AUDIO_INTERROMPIDO")
VOICE_RESULT_LABELS = {
    "SEM_ATENDIMENTO": "Sem atendimento",
    "SEM_RESPOSTA": "Sem resposta",
    "OCUPADO": "Linha ocupada",
    "FALHOU": "Falha de chamada / SIP",
    "FALHA_TRANSFERENCIA": "Erro na transferência",
    "FALHA_AUDIO": "Erro na reprodução de áudio",
    "ERRO": "Erro de execução",
    "TIMEOUT": "Tempo de chamada esgotado",
    "INTERROMPIDA": "Interrompida pelo administrador",
    "AUDIO_INTERROMPIDO": "Áudio interrompido antes do fim",
    "INDETERMINADO": "Resultado indeterminado após reinício",
    "SEM_INTERESSE": "Sem confirmação DTMF",
    "FINALIZADA": "Atendida e encerrada sem transferência",
    "TRANSFERIDA": "Transferida",
    "AUDIO_CONCLUIDO": "Áudio concluído",
    "ATENDIDA": "Atendida",
    "INICIANDO": "Iniciando chamada",
    "CHAMANDO": "Chamando",
    "TOCANDO": "Tocando",
    "TRANSFERINDO": "Transferindo",
    "REPRODUZINDO_AUDIO": "Reproduzindo áudio",
    "CONFIRMADO_DTMF": "DTMF confirmado",
    "BLOQUEADA": "Bloqueada",
}
COMMAND_LABELS = {
    "VOICE_CAMPAIGN_START_BLOCKED": "Tentativa de início bloqueada",
    "VOICE_CAMPAIGN_STARTED": "Início autorizado pelo administrador",
    "VOICE_CAMPAIGN_PAUSED": "Campanha pausada pelo administrador",
    "VOICE_CAMPAIGN_RESUMED": "Campanha retomada de onde parou",
    "VOICE_CAMPAIGN_RESUME_BLOCKED": "Tentativa de retomada bloqueada",
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
    if row.action in {"VOICE_CAMPAIGN_START_BLOCKED", "VOICE_CAMPAIGN_RESUME_BLOCKED"}:
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
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status.in_(TECHNICAL_FAILURE_STATES)),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status == "BLOQUEADA"),
        func.count(VoiceRecipient.id).filter(VoiceRecipient.status == "AUDIO_CONCLUIDO"),
    ).where(VoiceRecipient.campaign_id == campaign.id)).one()
    stats = dict(zip(
        ("total", "pending", "attempted", "in_progress", "answered",
         "transferred", "failed", "blocked", "audio_completed"), map(int, summary), strict=True,
    ))

    # Diagnóstico histórico: agrupamento por resultado persistido, inclusive
    # campanhas já pausadas. Não depende dos logs efêmeros do Railway.
    status_rows = db.execute(select(
        VoiceRecipient.status, func.count(VoiceRecipient.id),
    ).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.attempts > 0,
    ).group_by(VoiceRecipient.status)).all()
    status_counts = {str(code): int(count) for code, count in status_rows}
    stats.update({
        "no_answer": sum(status_counts.get(code, 0) for code in NO_ANSWER_STATES),
        "busy": status_counts.get("OCUPADO", 0),
        "timeout": status_counts.get("TIMEOUT", 0),
        "without_dtmf": status_counts.get("SEM_INTERESSE", 0),
        "interrupted": sum(status_counts.get(code, 0) for code in INTERRUPTED_STATES),
        "indeterminate": status_counts.get("INDETERMINADO", 0),
    })
    result_breakdown = [
        {"status": code, "label": VOICE_RESULT_LABELS.get(code, code.replace("_", " ").title()),
         "count": status_counts[code]}
        for code in sorted(status_counts, key=lambda status: (-status_counts[status], status))
    ]

    usage_rows = db.execute(select(
        VoiceRecipient.outbound_techprefix, func.count(VoiceRecipient.id),
    ).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.outbound_techprefix != "",
    ).group_by(VoiceRecipient.outbound_techprefix)).all()
    native_line_counts = {code: int(qty) for code, qty in usage_rows}

    attempts = list(db.scalars(select(VoiceRecipient).where(
        VoiceRecipient.campaign_id == campaign.id,
        VoiceRecipient.attempts > 0,
    ).order_by(VoiceRecipient.last_attempt_at.desc(), VoiceRecipient.id.desc()).limit(8)))
    recent_attempts = [{
        "name": row.name or "Contato",
        "phone_suffix": (row.phone or "")[-4:].rjust(4, "*"),
        "status": row.status,
        "origin_prefix": row.outbound_techprefix or "",
        "origin_number": NATIVE_ORIGIN_NUMBERS.get(row.outbound_techprefix, ""),
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
        "origin_prefix": recipient.outbound_techprefix or "",
        "origin_number": NATIVE_ORIGIN_NUMBERS.get(recipient.outbound_techprefix, ""),
        "at": _iso(event.created_at),
    } for event, recipient in event_rows]

    status = campaign.status
    next_window = None
    if status == "EM_EXECUCAO":
        if not stats["pending"] and not stats["in_progress"]:
            headline = "Sem contatos pendentes — aguardando conclusão da campanha"
        elif not within_call_window(campaign):
            next_window = next_call_window_start(campaign)
            if next_window:
                headline = (
                    "Aguardando horário — nenhuma ligação fora da janela permitida. "
                    f"Próxima discagem: {next_window.strftime('%d/%m/%Y às %H:%M')}"
                )
            else:
                headline = "Horário de campanha inválido — nenhuma chamada poderá ser iniciada"
        elif reasons:
            headline = "Em execução — temporariamente bloqueada: " + " / ".join(reasons[:2])
        else:
            headline = "Em execução — aguardando próxima chamada"
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
        "next_window_at": _iso(next_window),
        "outside_call_window": status == "EM_EXECUCAO" and not within_call_window(campaign),
        "readiness": list(reasons),
        "stats": stats,
        "result_breakdown": result_breakdown,
        "native_line_counts": native_line_counts,
        "outbound_routing_mode": campaign.outbound_routing_mode,
        "last_command": _last_command(db, campaign.id),
        "started_at": _iso(campaign.started_at),
        "last_call_at": _iso(campaign.last_call_at),
        "last_attempts": recent_attempts,
        "events": recent_events,
    }
