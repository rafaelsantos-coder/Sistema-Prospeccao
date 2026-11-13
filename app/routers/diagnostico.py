"""Diagnóstico interno somente leitura.

Rota protegida por segredo no caminho (DIAGNOSTICS_SECRET, mínimo 24 caracteres).
Sem a variável configurada a rota responde 404. Nunca devolve telefones, nomes,
mensagens, tokens ou chaves: apenas contagens, estados e identificadores técnicos.
"""

from __future__ import annotations

import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIQueueBinding,
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Conversation,
    ConversationZApiInstance,
    ProspectingJob,
    ServiceQueue,
    ServiceQueueZApiInstance,
    ZApiInstance,
)
from app.timeutils import utcnow


router = APIRouter(prefix="/internal/diag")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


@router.get("/{secret}")
def diagnostico(secret: str, db: Session = Depends(get_db)):
    expected = (settings.diagnostics_secret or "").strip()
    if len(expected) < 24 or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=404, detail="not found")

    now = utcnow()

    instances = {
        item.id: item
        for item in db.scalars(select(ZApiInstance))
    }
    queue_instance = {
        link.queue_id: link.zapi_instance_id
        for link in db.scalars(select(ServiceQueueZApiInstance))
    }
    agents = {item.id: item for item in db.scalars(select(AIAgentProfile))}
    bindings = {item.queue_id: item for item in db.scalars(select(AIQueueBinding))}

    queues = []
    for queue in db.scalars(select(ServiceQueue).order_by(ServiceQueue.id)):
        instance = instances.get(queue_instance.get(queue.id))
        binding = bindings.get(queue.id)
        agent = agents.get(binding.agent_id) if binding else None
        queues.append(
            {
                "id": queue.id,
                "name": queue.name,
                "code": queue.code,
                "team": queue.team,
                "active": queue.active,
                "default_inbound": queue.is_default_inbound,
                "zapi_instance": (
                    {
                        "id": instance.id,
                        "name": instance.name,
                        "instance_id_prefix": instance.instance_id[:6],
                        "active": instance.active,
                    }
                    if instance
                    else None
                ),
                "ai": (
                    {
                        "agent": agent.name if agent else None,
                        "role": agent.role_type if agent else None,
                        "agent_active": agent.active if agent else None,
                        "execution_mode": agent.execution_mode if agent else None,
                        "binding_active": binding.active,
                        "handling_mode": binding.handling_mode,
                        "fallback_to_human": binding.fallback_to_human,
                    }
                    if binding
                    else None
                ),
            }
        )

    dispatches = []
    for dispatch in db.scalars(select(CampaignDispatch).order_by(CampaignDispatch.id)):
        rows = db.execute(
            select(CampaignDispatchRecipient.status, func.count(CampaignDispatchRecipient.id))
            .where(CampaignDispatchRecipient.dispatch_id == dispatch.id)
            .group_by(CampaignDispatchRecipient.status)
        ).all()
        counts = {str(status): int(total) for status, total in rows}
        campaign = db.get(Campaign, dispatch.campaign_id)
        last_sent = db.scalar(
            select(func.max(CampaignDispatchRecipient.sent_at)).where(
                CampaignDispatchRecipient.dispatch_id == dispatch.id
            )
        )
        sent_24h = db.scalar(
            select(func.count(CampaignDispatchRecipient.id)).where(
                CampaignDispatchRecipient.dispatch_id == dispatch.id,
                CampaignDispatchRecipient.sent_at >= now - timedelta(hours=24),
            )
        )
        dispatches.append(
            {
                "id": dispatch.id,
                "campaign": campaign.name if campaign else None,
                "queue_id": dispatch.queue_id,
                "status": dispatch.status,
                "interval_seconds": dispatch.interval_seconds,
                "daily_limit": dispatch.daily_limit,
                "next_send_at": _iso(dispatch.next_send_at),
                "last_sent_at": _iso(last_sent),
                "sent_last_24h": int(sent_24h or 0),
                "recipients": counts,
            }
        )

    conv_by_status = {
        str(status): int(total)
        for status, total in db.execute(
            select(Conversation.status, func.count(Conversation.id)).group_by(Conversation.status)
        ).all()
    }
    closing_by_step = {
        str(step): int(total)
        for step, total in db.execute(
            select(AIClosingState.step, func.count(AIClosingState.id)).group_by(AIClosingState.step)
        ).all()
    }
    conv_instance = {
        str(instance_id): int(total)
        for instance_id, total in db.execute(
            select(
                ConversationZApiInstance.zapi_instance_id,
                func.count(ConversationZApiInstance.id),
            ).group_by(ConversationZApiInstance.zapi_instance_id)
        ).all()
    }

    prospecting_jobs = [
        {
            "id": job.id,
            "status": job.status,
            "limit": job.limit,
            "found": job.found_count,
            "imported": job.imported_count,
            "error": (job.error or "")[:300] or None,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        }
        for job in db.scalars(
            select(ProspectingJob).order_by(ProspectingJob.id.desc()).limit(10)
        )
    ]

    return {
        "generated_at": now.isoformat(),
        "prospecting_jobs": prospecting_jobs,
        "queues": queues,
        "dispatches": dispatches,
        "zapi_instances": [
            {
                "id": item.id,
                "name": item.name,
                "instance_id_prefix": item.instance_id[:6],
                "active": item.active,
                "source": item.source_type,
            }
            for item in instances.values()
        ],
        "conversations_by_status": conv_by_status,
        "conversations_by_zapi_instance": conv_instance,
        "ai_closing_by_step": closing_by_step,
        "ai_agents": [
            {
                "id": item.id,
                "name": item.name,
                "role": item.role_type,
                "mode": item.execution_mode,
                "active": item.active,
            }
            for item in agents.values()
        ],
        "flags": {
            "ai_sdr": settings.ai_sdr_enabled,
            "ai_seller": settings.ai_seller_enabled,
            "ai_autonomous": settings.ai_autonomous_enabled,
            "guardian_enforce": settings.ai_supervisor_guardian_enabled,
            "guardian_shadow": settings.ai_supervisor_guardian_shadow_enabled,
        },
    }
