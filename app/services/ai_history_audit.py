"""Auditoria SOMENTE LEITURA do histórico de respostas da IA (diagnóstico por log).

Registra, na subida do web, agregados e uma amostra de mensagens enviadas pela IA
(sem telefones/e-mails) para que o histórico possa ser analisado via logs do Railway.
Nunca escreve no banco e nunca impede a subida.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from app.models import AIAgentProfile, AIRun, Conversation, Lead, Message
from app.timeutils import utcnow

LOG = logging.getLogger("uvicorn.error")

_DIGITS = re.compile(r"\d{6,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
DAYS = 30
SAMPLE_CONVERSATIONS = 30
MAX_AI_MESSAGES_PER_CONVERSATION = 14
MAX_CHARS = 220


def _clean(text: str | None, limit: int = MAX_CHARS) -> str:
    value = _EMAIL.sub("<email>", text or "")
    value = _DIGITS.sub("<num>", value)
    return " ".join(value.split())[:limit]


def _weekly_conversations_query(ai_messages):
    """Usa um alias SQL único para agrupar e ordenar semanas no PostgreSQL.

    Repetir date_trunc("week", ...) cria parâmetros independentes no driver,
    e o PostgreSQL não considera essas expressões idênticas no GROUP BY.
    """
    week_start = func.date_trunc("week", Message.created_at).label("week_start")
    return (
        select(week_start, func.count(func.distinct(Message.conversation_id)))
        .where(ai_messages)
        .group_by("week_start")
        .order_by("week_start")
    )


def log_ai_history_audit(db) -> None:
    since = utcnow() - timedelta(days=DAYS)
    ai_user_ids = [row for row in db.scalars(select(AIAgentProfile.user_id)) if row]
    if not ai_user_ids:
        LOG.info("ai_audit sem_agentes_ia")
        return
    ai_messages = (Message.direction == "OUTBOUND") & (Message.sender_user_id.in_(ai_user_ids)) & (
        Message.created_at >= since
    )
    total = db.scalar(select(func.count()).select_from(Message).where(ai_messages)) or 0
    convs = db.scalar(select(func.count(func.distinct(Message.conversation_id))).where(ai_messages)) or 0
    inbound = db.scalar(
        select(func.count()).select_from(Message).where(Message.direction == "INBOUND", Message.created_at >= since)
    ) or 0
    LOG.info("ai_audit resumo dias=%s msgs_ia=%s conversas_com_ia=%s msgs_cliente=%s", DAYS, total, convs, inbound)

    for label, column in (("status", AIRun.status), ("intent", AIRun.intent), ("modo", AIRun.execution_mode), ("temp", AIRun.temperature)):
        rows = db.execute(select(column, func.count()).where(AIRun.created_at >= since).group_by(column)).all()
        LOG.info("ai_audit runs_por_%s %s", label, dict(sorted(((str(k), v) for k, v in rows), key=lambda x: -x[1])[:12]))
    handoff = db.scalar(select(func.count()).select_from(AIRun).where(AIRun.created_at >= since, AIRun.handoff_recommended.is_(True))) or 0
    LOG.info("ai_audit handoff_recomendado=%s", handoff)
    for err, qty in db.execute(
        select(AIRun.error, func.count()).where(AIRun.created_at >= since, AIRun.error.is_not(None)).group_by(AIRun.error).order_by(func.count().desc()).limit(8)
    ):
        LOG.info("ai_audit erro_run qty=%s msg=%s", qty, _clean(err, 160))
    for status, qty in db.execute(
        select(Conversation.status, func.count()).where(Conversation.id.in_(select(Message.conversation_id).where(ai_messages))).group_by(Conversation.status)
    ):
        LOG.info("ai_audit conversas_status status=%s qty=%s", status, qty)

    # Conversão: etapa do lead nas conversas atendidas pela IA e funil por etapa da conversa.
    ai_conversations = select(Message.conversation_id).where(ai_messages)
    for stage, qty in db.execute(
        select(Lead.stage, func.count(func.distinct(Conversation.id)))
        .join(Conversation, Conversation.lead_id == Lead.id)
        .where(Conversation.id.in_(ai_conversations))
        .group_by(Lead.stage)
        .order_by(func.count(func.distinct(Conversation.id)).desc())
    ):
        LOG.info("ai_audit conversao_lead_stage stage=%s conversas=%s", stage, qty)
    for label, term in (
        ("plano_apresentado", "%R$%"),
        ("pediu_cpf", "%CPF%"),
        ("pediu_cep", "%CEP%"),
        ("pediu_email", "%e-mail%"),
        ("contrato_assinatura", "%assinatura%"),
    ):
        qty = db.scalar(
            select(func.count(func.distinct(Message.conversation_id))).where(ai_messages, Message.body.ilike(term))
        ) or 0
        LOG.info("ai_audit funil etapa=%s conversas=%s de=%s", label, qty, convs)
    for week, qty in db.execute(_weekly_conversations_query(ai_messages)):
        LOG.info("ai_audit conversas_por_semana semana=%s conversas=%s", str(week)[:10], qty)

    bodies = Counter()
    for body in db.scalars(select(Message.body).where(ai_messages)):
        bodies[_clean(body, 120)] += 1
    for body, qty in bodies.most_common(20):
        LOG.info("ai_audit frase_repetida qty=%s texto=%s", qty, body)

    sample = db.execute(
        select(Message.conversation_id, func.count().label("n")).where(ai_messages).group_by(Message.conversation_id).order_by(func.max(Message.created_at).desc()).limit(SAMPLE_CONVERSATIONS)
    ).all()
    for conversation_id, qty in sample:
        inbound_qty = db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id, Message.direction == "INBOUND")) or 0
        LOG.info("ai_audit conversa id=%s msgs_ia=%s msgs_cliente=%s", conversation_id, qty, inbound_qty)
        for msg in db.scalars(
            select(Message).where(Message.conversation_id == conversation_id, Message.direction == "OUTBOUND", Message.sender_user_id.in_(ai_user_ids)).order_by(Message.created_at.asc()).limit(MAX_AI_MESSAGES_PER_CONVERSATION)
        ):
            LOG.info("ai_audit msg conv=%s t=%s tipo=%s st=%s texto=%s", conversation_id, msg.created_at.strftime("%d/%m %H:%M"), msg.message_type, msg.status, _clean(msg.body))
    LOG.info("ai_audit fim")
