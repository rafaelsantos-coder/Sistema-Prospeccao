from __future__ import annotations

import json
import logging
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIAgentProfile,
    AIConversationState,
    AIFollowupJob,
    Conversation,
    Lead,
    Message,
)
from app.services.ai_sdr_policy import SYSTEM_KNOWLEDGE_ITEMS, SULIA_SYSTEM_PROMPT, followup_spec, next_business_time
from app.services.audit import write_audit
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_followups")


class FollowupDecision(BaseModel):
    reply_text: str = Field(min_length=1, max_length=1200)
    rationale: str = Field(default="", max_length=500)


def cancel_pending_followups(
    db: Session,
    conversation_id: int,
    *,
    reason: str = "CONTEXTO_ALTERADO",
) -> int:
    jobs = list(
        db.scalars(
            select(AIFollowupJob).where(
                AIFollowupJob.conversation_id == conversation_id,
                AIFollowupJob.status.in_({"PENDENTE", "PROCESSANDO"}),
            )
        )
    )
    for job in jobs:
        job.status = "CANCELADO"
        job.error = reason[:2000]
    if jobs:
        db.flush()
    return len(jobs)


def schedule_followup(
    db: Session,
    *,
    agent_id: int,
    queue_id: int,
    conversation_id: int,
    source_message_id: int | None,
    step: int = 1,
) -> AIFollowupJob | None:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return None
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    if lead and (lead.do_not_contact or lead.stage in {"GANHO", "NAO_CONTATAR"}):
        return None
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    if state and state.state in {"HANDOFF", "HUMAN_ACTIVE", "OPT_OUT"}:
        return None

    cancel_pending_followups(db, conversation_id, reason="FOLLOWUP_REAGENDADO")
    delay, theme = followup_spec(step)
    due = next_business_time(utcnow() + delay, settings.default_timezone)
    job = AIFollowupJob(
        agent_id=agent_id,
        queue_id=queue_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        step=max(1, step),
        theme=theme,
        status="PENDENTE",
        due_at=due,
    )
    db.add(job)
    db.flush()
    return job


def claim_followup_job(db: Session) -> AIFollowupJob | None:
    job = db.scalar(
        select(AIFollowupJob)
        .where(
            AIFollowupJob.status == "PENDENTE",
            AIFollowupJob.due_at <= utcnow(),
        )
        .order_by(AIFollowupJob.due_at.asc(), AIFollowupJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not job:
        return None
    job.status = "PROCESSANDO"
    job.locked_at = utcnow()
    job.attempts = int(job.attempts or 0) + 1
    db.flush()
    return job


def _recent_history(db: Session, conversation_id: int, limit: int = 30) -> list[dict]:
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction.in_({"INBOUND", "OUTBOUND"}),
            )
            .order_by(Message.id.desc())
            .limit(max(6, min(limit, 60)))
        )
    )
    rows.reverse()
    return [
        {
            "id": item.id,
            "direction": item.direction,
            "body": item.body,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in rows
    ]


def _latest_inbound_id(db: Session, conversation_id: int) -> int | None:
    return db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


def _followup_allowed(db: Session, job: AIFollowupJob) -> tuple[bool, str]:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        return False, "CONVERSA_INEXISTENTE"
    if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return False, "CONVERSA_ENCERRADA"
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    if lead and lead.do_not_contact:
        return False, "DO_NOT_CONTACT"
    if lead and lead.stage in {"GANHO", "NAO_CONTATAR"}:
        return False, f"LEAD_{lead.stage}"
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    if state and state.state in {"HANDOFF", "HUMAN_ACTIVE", "OPT_OUT"}:
        return False, state.state
    latest_inbound = _latest_inbound_id(db, conversation.id)
    if (
        job.source_message_id
        and latest_inbound
        and int(latest_inbound) > int(job.source_message_id)
    ):
        return False, "CLIENTE_RESPONDEU"
    return True, ""


def _generate_followup(
    db: Session,
    *,
    agent: AIAgentProfile,
    job: AIFollowupJob,
) -> FollowupDecision:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")

    from agents import Agent, Runner

    conversation = db.get(Conversation, job.conversation_id)
    lead = db.get(Lead, conversation.lead_id) if conversation and conversation.lead_id else None
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == job.conversation_id
        )
    )
    knowledge = "\n\n".join(
        f"[{item['category']}] {item['title']}: {item['content']}"
        for item in SYSTEM_KNOWLEDGE_ITEMS
    )
    context = {
        "followup_step": job.step,
        "followup_theme": job.theme,
        "lead": {
            "city": lead.city if lead else "",
            "stage": lead.stage if lead else "",
            "notes": lead.notes if lead else "",
            "potential": lead.potential if lead else "",
        },
        "ai_state": {
            "summary": state.summary if state else "",
            "last_intent": state.last_intent if state else "UNKNOWN",
            "temperature": state.temperature if state else "FRIO",
        },
        "history": _recent_history(db, job.conversation_id),
    }
    prompt = (
        "Crie APENAS a próxima mensagem de follow-up comercial para WhatsApp. "
        "Ela deve retomar a conversa com naturalidade, usar o contexto real, ter um motivo comercial e "
        "estimular resposta. Não repita mensagens anteriores. Não aumente desconto por silêncio. "
        "Não invente urgência. Não diga 'viu minha mensagem?'. Faça no máximo uma pergunta principal. "
        "Se a base não sustentar uma afirmação, não a use.\n\n"
        f"OBJETIVO DESTA ETAPA: {job.theme}\n\n"
        f"CONTEXTO: {json.dumps(context, ensure_ascii=False, default=str)}\n\n"
        f"BASE OFICIAL: {knowledge}"
    )
    followup_agent = Agent(
        name=f"{agent.name} Follow-up",
        instructions=(agent.system_prompt or "").strip() or SULIA_SYSTEM_PROMPT,
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 45.0,
        },
        output_type=FollowupDecision,
    )
    result = Runner.run_sync(followup_agent, prompt)
    value = result.final_output
    if not isinstance(value, FollowupDecision):
        value = FollowupDecision.model_validate(value)
    return value


def process_followup_job(db: Session, job: AIFollowupJob) -> str:
    allowed, reason = _followup_allowed(db, job)
    if not allowed:
        job.status = "CANCELADO"
        job.error = reason
        return "cancelled"

    if not settings.ai_autonomous_enabled:
        job.status = "PENDENTE"
        job.due_at = utcnow() + timedelta(minutes=15)
        job.locked_at = None
        return "waiting_auto"

    agent = db.get(AIAgentProfile, job.agent_id)
    if not agent or not agent.active:
        job.status = "CANCELADO"
        job.error = "AGENTE_INATIVO"
        return "cancelled"

    try:
        decision = _generate_followup(db, agent=agent, job=job)
        conversation = db.get(Conversation, job.conversation_id)
        if not conversation or not conversation.contact or not conversation.contact.phone:
            raise RuntimeError("Conversa sem telefone para follow-up.")

        from app.services.conversation_engine import register_message
        from app.services.zapi_instances import service_for_conversation

        result = service_for_conversation(db, conversation.id).send_text(
            phone=conversation.contact.phone,
            message=decision.reply_text,
        )
        provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
        zaap_id = str(result.get("zaapId") or "").strip() or None
        message = register_message(
            db,
            conversation,
            direction="OUTBOUND",
            message_type="TEXT",
            body=decision.reply_text,
            sender_user_id=agent.user_id,
            provider_message_id=provider_id,
            provider_zaap_id=zaap_id,
            status="SENT",
        )
        db.flush()
        job.status = "ENVIADO"
        job.sent_at = utcnow()
        job.error = None

        write_audit(
            db,
            user=agent.user,
            action="AI_SDR_FOLLOWUP_SENT",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "followup_job_id": job.id,
                "step": job.step,
                "theme": job.theme,
                "message_id": message.id,
            },
        )

        schedule_followup(
            db,
            agent_id=job.agent_id,
            queue_id=job.queue_id,
            conversation_id=job.conversation_id,
            source_message_id=job.source_message_id,
            step=job.step + 1,
        )
        return "sent"
    except Exception as exc:
        job.error = str(exc)[:2000]
        if int(job.attempts or 0) < 3:
            job.status = "PENDENTE"
            job.locked_at = None
            job.due_at = utcnow() + timedelta(minutes=5 * int(job.attempts or 1))
        else:
            job.status = "FALHOU"
        LOG.exception("Falha no follow-up IA job=%s", job.id)
        return "failed"
