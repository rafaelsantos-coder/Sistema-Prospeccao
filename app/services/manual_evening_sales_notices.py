"""Avisos individuais ao gestor para revisão de conversas após 18h de 23/11.

Solicitação pontual do gestor em 23/11/2026. Não envia nada para clientes,
não altera atribuições e não lê histórico de outra conversa para construir
cada alerta. Usa o canal auditado da Z-API e deduplicação por conversa.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIClosingState, AIConversationState, AIRun, Contact,
    Conversation, Lead, Message, User,
)
from app.services.auto_reply import is_automatic_inbound
from app.services.manual_human_review_notices import _clock, _safe_excerpt
from app.services.ops_alerts import send_ops_alert
from app.services.queue_routing import queue_for_conversation

LOG = logging.getLogger("sulnet_evening_sales_notices")
RECIPIENT = "55999002508"  # número do gestor: DDD 55 sem DDI; Z-API insere 55
CUTOFF_UTC = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
# IDs confirmados nos logs de mensagens recebidas entre 18h e 20h19 BRT.
CASES = (4557, 5607, 5589)
KEY_PREFIX = "manual-sales-review:20261123-from18h"


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _last(db: Session, conversation_id: int, direction: str) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == direction,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


def _eligible(
    *,
    conversation: Conversation | None,
    inbound: Message | None,
    outbound: Message | None,
    lead: Lead | None,
) -> tuple[bool, str]:
    """Só notificar uma entrada humana recente sem resposta efetiva posterior."""
    if conversation is None or inbound is None:
        return False, "not_found"
    if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return False, "closed"
    if lead and (lead.do_not_contact or lead.stage == "NAO_CONTATAR"):
        return False, "do_not_contact"
    if inbound.message_type not in {"TEXT", "BUTTONS", "AUDIO", "IMAGE", "DOCUMENT", "VIDEO"}:
        return False, "unsupported_media"
    when = _timestamp(inbound.created_at)
    if when is None or when < CUTOFF_UTC:
        return False, "out_of_window"
    if is_automatic_inbound(inbound):
        return False, "automatic_reply"
    if (
        outbound is not None
        and outbound.id > inbound.id
        and outbound.status in {"SENT", "DELIVERED", "READ", "RECEIVED"}
    ):
        return False, "already_replied"
    return True, "pending_human_review"


def _notice(
    db: Session,
    conversation: Conversation,
    inbound: Message,
    outbound: Message | None,
    *,
    index: int,
    total: int,
) -> str:
    contact = db.get(Contact, conversation.contact_id) if conversation.contact_id else None
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    assigned = db.get(User, conversation.assigned_user_id) if conversation.assigned_user_id else None
    queue = queue_for_conversation(db, conversation.id)
    state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id).limit(1)
    )
    closer = db.scalar(
        select(AIClosingState).where(AIClosingState.conversation_id == conversation.id).limit(1)
    )
    run = db.scalar(
        select(AIRun)
        .where(AIRun.conversation_id == conversation.id)
        .order_by(AIRun.id.desc()).limit(1)
    )
    name = (
        str((contact.name or contact.business_name) if contact else "")
        or str((lead.contact_name or lead.business_name) if lead else "")
        or "(nome não cadastrado)"
    ).strip()
    phone = (contact.phone if contact else "") or (lead.phone if lead else "") or "(sem telefone cadastrado)"
    crm_stage = str(lead.stage if lead else "sem CRM local")
    temp = str((lead.temperature if lead else None) or (state.temperature if state else None) or "não identificada")
    owner = assigned.name if assigned else "sem vendedor atribuído"
    ai_state = str(state.state if state else "sem estado de IA")
    plan = str(closer.plan_label or "") if closer else ""
    closing_step = str(closer.step or "") if closer else ""
    last_text = _safe_excerpt(inbound.body, limit=270)
    last_out = (
        f"{_clock(outbound)} | {outbound.status} | {_safe_excerpt(outbound.body, limit=160)}"
        if outbound else "(não localizada)"
    )
    if ai_state in {"HUMAN_ACTIVE", "HANDOFF", "HANDOFF_HUMAN", "ESCALATED"}:
        reason = "Em atendimento humano; última mensagem do cliente não tem resposta posterior confirmada."
        action = "Confirmar com o vendedor responsável se houve retorno; se não houve, atender sem duplicar contato."
    elif outbound and outbound.id > inbound.id and outbound.status in {"CANCELED", "FAILED"}:
        reason = f"Resposta de saída posterior foi {outbound.status}; cliente pode ter ficado sem retorno."
        action = "Verificar o motivo do cancelamento/falha e responder a dúvida; assumir manualmente se necessário."
    else:
        reason = "Última mensagem recebida sem resposta efetiva posterior registrada no Prospecta."
        action = "Vendedor verificar a última dúvida e chamar o cliente se ainda estiver pendente."
    if closing_step and closing_step not in {"COMPLETED", "ESCALATED"}:
        action += f" Conferir etapa de fechamento {closing_step} no CRM."
    if plan:
        plan_text = f"*Plano em fechamento:* {_safe_excerpt(plan, limit=120)}\n"
    else:
        plan_text = ""
    summary = _safe_excerpt(run.summary, limit=150) if run and run.summary else ""
    detail = f"*Último resumo da IA (a conferir):* {summary}\n" if summary else ""
    link = (
        f"{settings.app_public_url.rstrip('/')}/conversations/{conversation.id}"
        if settings.app_public_url else f"Conversa #{conversation.id} no Prospecta"
    )
    return (
        f"🧡 *PROSPECTA | REVISÃO COMERCIAL {index}/{total}*\n"
        "*Período:* desde 23/11 às 18h (Brasília)\n"
        f"*Conversa:* #{conversation.id} | *Fila:* {queue.name if queue else 'não localizada'}\n"
        f"*Cliente:* {name}\n"
        f"*Telefone do cliente:* {phone}\n"
        f"*CRM:* {crm_stage} | *Temperatura:* {temp}\n"
        f"*Responsável:* {owner} | *Estado da IA:* {ai_state}\n"
        f"{plan_text}"
        f"*Última mensagem do cliente ({_clock(inbound)}):* {last_text}\n"
        f"*Última saída:* {last_out}\n"
        f"{detail}"
        f"*Onde parou:* {reason}\n"
        f"*Próxima ação:* {action}\n"
        f"*Abrir no Prospecta:* {link}\n"
        "⚠️ Status de oportunidade de venda depende da verificação do contexto; não presumir venda fechada."
    )


def notify_evening_sales_review(db: Session) -> dict[str, object]:
    """Uma mensagem por caso elegível para o WhatsApp autorizado do gestor."""
    totals: dict[str, object] = {"sent": 0, "skipped": {}, "not_sent": 0}
    eligible = []
    for cid in CASES:
        conversation = db.get(Conversation, cid)
        inbound = _last(db, cid, "INBOUND")
        outbound = _last(db, cid, "OUTBOUND")
        lead = db.get(Lead, conversation.lead_id) if conversation and conversation.lead_id else None
        allowed, reason = _eligible(conversation=conversation, inbound=inbound, outbound=outbound, lead=lead)
        if not allowed:
            skipped = totals["skipped"]
            skipped[reason] = skipped.get(reason, 0) + 1
            LOG.info("sales_review_skipped conversation=%s reason=%s", cid, reason)
            continue
        eligible.append((cid, conversation, inbound, outbound))
    for index, (cid, conversation, inbound, outbound) in enumerate(eligible, start=1):
        try:
            text = _notice(db, conversation, inbound, outbound, index=index, total=len(eligible))
            sent = send_ops_alert(
                db,
                key=f"{KEY_PREFIX}:{cid}",
                text=text,
                recipient_phone=RECIPIENT,
            )
            if sent:
                totals["sent"] += 1
                LOG.warning("sales_review_notice_sent conversation=%s destination_last4=2508", cid)
            else:
                totals["not_sent"] += 1
                LOG.warning("sales_review_notice_unsent_or_duplicate conversation=%s", cid)
        except Exception:
            db.rollback()
            totals["not_sent"] += 1
            LOG.exception("sales_review_notice_failed conversation=%s", cid)
    LOG.warning("sales_review_notification_summary=%s", json.dumps(totals, ensure_ascii=False))
    return totals
