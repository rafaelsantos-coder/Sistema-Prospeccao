"""Envio pontual de dois alertas solicitados pelo gestor, via Z-API.

Nao e um alerta recorrente nem altera a posse das conversas. Usa os dados
reais do Postgres e o envio auditado da rotina de avisos operacionais.
"""
from __future__ import annotations

import logging
import re
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIConversationState, AIRun, Contact, Conversation, Message, User
from app.services.ai_sdr import human_is_attending
from app.services.auto_reply import is_automatic_inbound
from app.services.ops_alerts import send_ops_alert
from app.services.queue_routing import queue_for_conversation

LOG = logging.getLogger("sulnet_manual_human_review_notices")
RECIPIENT = "55999002508"  # DDD 55; ZApiService inclui DDI 55
CASES = (4802, 4887)
NOTICE_KEY_PREFIX = "human-review:20261122:one-off"
_BRAZIL = ZoneInfo("America/Sao_Paulo")


def _safe_excerpt(body: str | None, *, limit: int = 250) -> str:
    text = " ".join(str(body or "").strip().split())
    text = re.sub(
        r"(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}[-\s]?\d{2}(?!\d)",
        "[CPF ocultado]", text,
    )
    text = re.sub(r"(?i)(senha|password|token)\s*[:=]\s*\S+", r"\1: [oculto]", text)
    if not text:
        return "(sem texto; ver mídia no Prospecta)"
    return text[:limit] + ("…" if len(text) > limit else "")


def _clock(message: Message | None) -> str:
    if message is None or message.created_at is None:
        return "horário não registrado"
    when = message.created_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_BRAZIL).strftime("%d/%m %H:%M")


def _case_text(db: Session, conversation_id: int, index: int, total: int) -> str:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        return (
            f"🧡 *SULNET PROSPECTA | Aviso {index}/{total}*\n"
            f"*Conversa:* #{conversation_id}\n"
            "*Status:* conversa não localizada no banco.\n"
            "*Ação humana:* conferir cadastro e origem do alerta. "
            "Nenhum contato foi notificado."
        )

    contact = db.get(Contact, conversation.contact_id) if conversation.contact_id else None
    person = db.get(User, conversation.assigned_user_id) if conversation.assigned_user_id else None
    queue = queue_for_conversation(db, conversation_id)
    state = db.scalar(select(AIConversationState).where(
        AIConversationState.conversation_id == conversation_id,
    ).limit(1))
    run = db.scalar(select(AIRun).where(
        AIRun.conversation_id == conversation_id,
    ).order_by(AIRun.id.desc()).limit(1))
    inbound = db.scalar(select(Message).where(
        Message.conversation_id == conversation_id,
        Message.direction == "INBOUND",
    ).order_by(Message.id.desc()).limit(1))
    latest = db.scalar(select(Message).where(
        Message.conversation_id == conversation_id,
        Message.direction.in_(("INBOUND", "OUTBOUND")),
    ).order_by(Message.id.desc()).limit(1))
    last_outbound = db.scalar(select(Message).where(
        Message.conversation_id == conversation_id,
        Message.direction == "OUTBOUND",
    ).order_by(Message.id.desc()).limit(1))

    if inbound and is_automatic_inbound(inbound):
        reason = (
            "A última entrada foi classificada pelo filtro como *resposta automática*. "
            "A SulIA não abriu um novo atendimento para evitar troca entre robôs."
        )
        action = (
            "Revisar o conteúdo: se for uma mensagem de pessoa interessada, "
            "corrigir a classificação e dar continuidade; se for robô, não insistir."
        )
    elif state and state.state in {"HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "ESCALATED"}:
        reason = "Conversa com estado de transferência para atendimento humano."
        if run and run.handoff_reason:
            reason += " Motivo registrado: " + _safe_excerpt(run.handoff_reason, limit=180)
        action = "Assumir no Prospecta, analisar histórico e responder a pendência real."
    elif human_is_attending(db, conversation):
        reason = "Há um vendedor atribuído com intervenção humana registrada."
        action = "Confirmar que o vendedor está acompanhando e responder à última dúvida."
    elif latest and latest.direction == "OUTBOUND":
        reason = (
            "Existe mensagem de saída após a última entrada; "
            f"status dessa saída: {latest.status}. Não é possível afirmar que ainda aguarda humano."
        )
        action = "Verificar confirmação de entrega e se há nova pendência."
    else:
        reason = (
            "Última mensagem recebida sem envio posterior confirmado no histórico. "
            "Revisão humana necessária para identificar o motivo do bloqueio."
        )
        action = "Revisar histórico e atender ou devolver à SulIA quando elegível."

    name = (contact.name or contact.business_name or "").strip() if contact else ""
    name = name or "(nome não informado)"
    phone = (contact.phone or "").strip() if contact else "(telefone não cadastrado)"
    crm_stage = (
        (conversation.lead.stage if conversation.lead else "")
        or (contact.lead.stage if contact and contact.lead else "")
        or "sem etapa CRM registrada"
    )
    owner = person.name if person else "sem responsável atribuído"
    ai_status = state.state if state else "sem estado de IA registrado"
    last_received = _safe_excerpt(inbound.body) if inbound else "sem entrada encontrada"
    last_reply = (
        f"{_clock(last_outbound)} | {last_outbound.status} | "
        f"{_safe_excerpt(last_outbound.body, limit=130)}"
        if last_outbound else "nenhuma mensagem de saída localizada"
    )
    return (
        f"🧡 *SULNET PROSPECTA | Aviso {index}/{total}*\n"
        f"*Conversa:* #{conversation_id} | *Fila:* {queue.name if queue else 'não localizada'}\n"
        f"*Contato:* {name}\n"
        f"*Telefone:* {phone}\n"
        f"*Status:* {conversation.status} | *CRM:* {crm_stage}\n"
        f"*Responsável:* {owner} | *IA:* {ai_status}\n"
        f"*Última entrada ({_clock(inbound)}):* {last_received}\n"
        f"*Última saída:* {last_reply}\n"
        f"*Onde parou:* {reason}\n"
        f"*Ação humana:* {action}\n"
        f"*Abrir no Prospecta:* conversa #{conversation_id}"
    )


def notify_two_human_review_cases(db: Session) -> dict[str, int]:
    """Envia mensagens separadas com chave estável de deduplicação."""
    counts = {"sent": 0, "not_sent_or_already_sent": 0}
    for index, cid in enumerate(CASES, start=1):
        body = _case_text(db, cid, index, len(CASES))
        try:
            sent = send_ops_alert(
                db,
                key=f"{NOTICE_KEY_PREFIX}:{cid}",
                text=body,
                recipient_phone=RECIPIENT,
            )
        except Exception:
            LOG.exception("human_review_notice_error conversation=%s", cid)
            sent = False
        if sent:
            counts["sent"] += 1
            LOG.warning("human_review_notice_sent conversation=%s destination_last4=2508", cid)
        else:
            counts["not_sent_or_already_sent"] += 1
            LOG.warning("human_review_notice_not_sent_or_duplicate conversation=%s", cid)
    return counts
