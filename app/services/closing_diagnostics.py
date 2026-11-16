"""Diagnóstico somente leitura dos fechamentos que esperam o cliente (por log).

Mostra, por venda, a etapa, o estado da IA, se a fila tem IA, e os últimos
lembretes (status, vencimento, motivo de cancelamento). Sem nome, telefone ou CPF.
Criado para descobrir por que o lembrete da confirmação não saiu (16/11).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.models import AIClosingState, AIConversationState, AIFollowupJob, Message
from app.timeutils import utcnow

LOG = logging.getLogger("uvicorn.error")


def log_closing_diagnostics(db) -> None:
    from app.services.ai_sdr import binding_for_conversation  # noqa: PLC0415
    from app.services.ai_seller import SELLER_CUSTOMER_INPUT_STEPS  # noqa: PLC0415

    states = db.scalars(
        select(AIClosingState).where(
            AIClosingState.completed_at.is_(None),
            AIClosingState.updated_at >= utcnow() - timedelta(days=7),
        )
    )
    for state in states:
        ai_state = db.scalar(
            select(AIConversationState).where(AIConversationState.conversation_id == state.conversation_id)
        )
        last = db.scalar(
            select(Message)
            .where(Message.conversation_id == state.conversation_id)
            .order_by(Message.id.desc())
            .limit(1)
        )
        jobs = list(
            db.scalars(
                select(AIFollowupJob)
                .where(AIFollowupJob.conversation_id == state.conversation_id)
                .order_by(AIFollowupJob.id.desc())
                .limit(4)
            )
        )
        LOG.info(
            "closing_diag state=%s conversation=%s step=%s aguarda_cliente=%s ai_state=%s ai_agent=%s "
            "closing_agent=%s fila_com_ia=%s ultima_msg=%s@%s lembretes=%s",
            state.id,
            state.conversation_id,
            state.step,
            state.step in SELLER_CUSTOMER_INPUT_STEPS,
            ai_state.state if ai_state else None,
            ai_state.agent_id if ai_state else None,
            state.agent_id,
            binding_for_conversation(db, state.conversation_id) is not None,
            last.direction if last else None,
            last.created_at if last else None,
            [
                f"{job.id}:{job.status}:step{job.step}:due={job.due_at}:err={(job.error or '-')[:60]}"
                for job in jobs
            ],
        )
