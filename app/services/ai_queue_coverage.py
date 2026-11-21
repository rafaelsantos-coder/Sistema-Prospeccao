"""Habilitacao unica da cobertura de IA em todas as filas WhatsApp ativas.

Solicitacao operacional de 16/11/2026: assumir mensagens nao respondidas
quando as vendedoras nao estiverem online, sem migrar de numero nem interferir
em conversas ja atendidas por humanos. Nunca muda o cadastro da Z-API.
"""

from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIAgentProfile, AIQueueBinding, AuditLog, ServiceQueue
from app.services.ai_queue_scope import ai_allowed_for_queue, is_general_commercial_queue, manual_queue_control_enabled
from app.services.audit import write_audit
from app.services.queue_routing import AI_WHEN_HUMAN_IDLE
from app.services.zapi_instances import queue_instance


LOG = logging.getLogger("sulnet_ai_queue_coverage")

# Chave persistida: futuras reinicializacoes nao revertem o que o gestor
# decidir posteriormente (pausar uma fila, mudar IA ou modo de atendimento).
COVERAGE_CAMPAIGN_KEY = "all-queues-unread-20261116"
COVERAGE_ACTION = "AI_ALL_QUEUES_COVERAGE_APPLIED"


def ensure_unanswered_coverage_all_queues(db: Session) -> dict[str, object]:
    # Rotina legada de auto-habilitacao: proibida sob controle manual do gestor.
    if manual_queue_control_enabled():
        return {"already_applied": True, "enabled": [], "without_whatsapp": [], "manual_control": True}
    already = db.scalar(
        select(AuditLog.id).where(
            AuditLog.action == COVERAGE_ACTION,
            AuditLog.entity_id == COVERAGE_CAMPAIGN_KEY,
        ).limit(1)
    )
    if already:
        return {"already_applied": True, "enabled": [], "without_whatsapp": []}

    # Escolher um SDR capaz de enviar, nao um agente em shadow nem o Vendedor IA.
    agents = list(
        db.scalars(
            select(AIAgentProfile).where(
                AIAgentProfile.role_type == "SDR",
                AIAgentProfile.active.is_(True),
                AIAgentProfile.execution_mode == "AUTO",
            ).order_by(AIAgentProfile.id.asc())
        )
    )
    agent = next((item for item in agents if item.slug == "sulia-sdr-digital"), None)
    if agent is None:
        agent = agents[0] if agents else None
    if agent is None:
        LOG.error("ai_all_queues_coverage_missing_autonomous_sdr")
        return {"already_applied": False, "enabled": [], "without_whatsapp": [], "error": "missing_auto_sdr"}

    enabled: list[int] = []
    without_whatsapp: list[int] = []
    preserved: list[int] = []
    queues = list(
        db.scalars(
            select(ServiceQueue)
            .where(ServiceQueue.active.is_(True))
            .order_by(ServiceQueue.id.asc())
        )
    )
    for queue in queues:
        if is_general_commercial_queue(queue) or not ai_allowed_for_queue(queue.id):
            continue
        if not queue_instance(db, queue.id):
            without_whatsapp.append(queue.id)
            continue
        binding = db.scalar(
            select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id)
        )
        if binding and binding.active:
            existing_agent = db.get(AIAgentProfile, binding.agent_id)
            if (
                binding.handling_mode != "HUMAN"
                and existing_agent
                and existing_agent.active
                and existing_agent.role_type == "SDR"
                and existing_agent.execution_mode == "AUTO"
            ):
                preserved.append(queue.id)
                continue
        if binding is None:
            binding = AIQueueBinding(
                queue_id=queue.id,
                agent_id=agent.id,
                handling_mode=AI_WHEN_HUMAN_IDLE,
                active=True,
                fallback_to_human=True,
            )
            db.add(binding)
        else:
            binding.agent_id = agent.id
            binding.handling_mode = AI_WHEN_HUMAN_IDLE
            binding.active = True
            binding.fallback_to_human = True
        enabled.append(queue.id)
        write_audit(
            db,
            user=None,
            action="AI_QUEUE_UNANSWERED_COVERAGE_ENABLED",
            entity_type="service_queue",
            entity_id=queue.id,
            payload={
                "mode": AI_WHEN_HUMAN_IDLE,
                "agent_id": agent.id,
                "reason": "offline_or_human_unanswered",
            },
        )

    write_audit(
        db,
        user=None,
        action=COVERAGE_ACTION,
        entity_type="ai_queue_coverage",
        entity_id=COVERAGE_CAMPAIGN_KEY,
        payload={
            "enabled_queues": enabled,
            "preserved_queues": preserved,
            "without_whatsapp": without_whatsapp,
            "agent_id": agent.id,
        },
    )
    db.commit()
    LOG.warning(
        "ai_all_queues_coverage_applied enabled=%s preserved=%s no_whatsapp=%s agent=%s",
        enabled,
        preserved,
        without_whatsapp,
        agent.id,
    )
    return {
        "already_applied": False,
        "enabled": enabled,
        "preserved": preserved,
        "without_whatsapp": without_whatsapp,
    }
