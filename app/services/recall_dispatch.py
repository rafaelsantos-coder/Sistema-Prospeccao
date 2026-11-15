"""Monta o disparo da rechamada (ai_campaign_recall) para quem a IA atendeu.

O disparo nasce em RASCUNHO, um por fila, para o gestor conferir e iniciar na
tela de Disparos. Nada é enviado aqui. Controlado por RECALL_IA_DISPATCH:
"dry_run" só registra as contagens no log; "prepare" cria os rascunhos.
Rodar de novo não duplica: quem já está num disparo da rechamada fica de fora.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import timedelta

from sqlalchemy import select, text

from app.models import (
    AIAgentProfile,
    AIConversationState,
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Conversation,
    ConversationQueueLink,
    Lead,
    Message,
    ServiceQueue,
)
from app.services import ai_campaign_recall as recall
from app.services.scoring import normalize_phone
from app.timeutils import utcnow

LOG = logging.getLogger("uvicorn.error")

DAYS = 7
INTERVAL_SECONDS = 60
DAILY_LIMIT = 150
WINDOW_START = "08:00"
WINDOW_END = "21:00"
_LOCK_ID = 771_511_2026
# Conversa que já saiu do SDR: a resposta do cliente não chegaria à rechamada.
_BLOCKED_AI_STATES = {"CLOSING", "OPT_OUT", "COMPLETED", "HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "ESCALATED"}


def _campaign(db, *, create: bool) -> Campaign | None:
    campaign = db.scalar(select(Campaign).where(Campaign.name == recall.CAMPAIGN_NAME).limit(1))
    if campaign or not create:
        return campaign
    campaign = Campaign(
        name=recall.CAMPAIGN_NAME,
        channel="WHATSAPP",
        status="APROVADA",
        daily_limit=DAILY_LIMIT,
        sequence_json=json.dumps(
            [
                {
                    "day": 1,
                    "label": "Rechamada",
                    "message": recall.QUESTION_TEXT,
                    "media": [],
                    "quick_replies": [dict(item) for item in recall.QUESTION_BUTTONS],
                }
            ],
            ensure_ascii=False,
        ),
    )
    db.add(campaign)
    db.flush()
    return campaign


def _already_in_recall(db, campaign: Campaign | None) -> set[str]:
    if not campaign:
        return set()
    return {
        phone
        for phone in db.scalars(
            select(CampaignDispatchRecipient.phone)
            .join(CampaignDispatch, CampaignDispatch.id == CampaignDispatchRecipient.dispatch_id)
            .where(CampaignDispatch.campaign_id == campaign.id)
        )
        if phone
    }


def _exclusion(db, conversation: Conversation, lead: Lead | None, queue: ServiceQueue | None) -> str:
    from app.services.ai_sdr import (  # noqa: PLC0415
        _confirmed_customer_type_from_lead,
        _confirmed_existing_customer_from_lead,
        _ftth_plan_by_speed,
        _plan_available_in_city,
        _service_city_in_text,
        human_is_attending,
        human_stepped_in,
        queue_ai_binding,
    )

    if not lead or not normalize_phone(lead.phone):
        return "sem_lead_ou_telefone"
    if lead.do_not_contact or lead.stage == "NAO_CONTATAR":
        return "nao_contatar"
    if lead.stage == "GANHO":
        return "ja_fechou"
    if lead.stage == "PERDIDO":
        return "perdido"
    if conversation.status == "BLOQUEADO":
        return "conversa_bloqueada"
    if _confirmed_existing_customer_from_lead(lead) == "CLIENTE":
        return "ja_e_cliente"
    if _confirmed_customer_type_from_lead(lead) == "EMPRESA":
        return "empresa"
    state = db.scalar(
        select(AIConversationState.state).where(AIConversationState.conversation_id == conversation.id)
    )
    if state in _BLOCKED_AI_STATES:
        return "em_fechamento_ou_fora_da_ia"
    if human_is_attending(db, conversation) or human_stepped_in(db, conversation):
        return "vendedor_humano_atendendo"
    if not queue or not queue.active or not queue_ai_binding(db, queue.id):
        return "fila_sem_ia"
    city = _service_city_in_text(lead.city or "")
    plan = _ftth_plan_by_speed(recall.PLAN_SPEED)
    if city and plan and not _plan_available_in_city(plan, city):
        return "cidade_sem_800_mega"
    return ""


def prepare_recall_dispatches(db, *, dry_run: bool = True) -> dict:
    """Seleciona os clientes e, fora do dry_run, cria um disparo em rascunho por fila."""
    from app.services.dispatch_queue import create_dispatch_from_leads  # noqa: PLC0415

    since = utcnow() - timedelta(days=DAYS)
    ai_user_ids = [row for row in db.scalars(select(AIAgentProfile.user_id)) if row]
    result: dict = {"dry_run": dry_run, "conversas": 0, "elegiveis": 0, "excluidos": {}, "disparos": []}
    if not ai_user_ids:
        return result

    conversation_ids = list(
        db.scalars(
            select(Message.conversation_id)
            .where(
                Message.direction == "OUTBOUND",
                Message.sender_user_id.in_(ai_user_ids),
                Message.created_at >= since,
            )
            .distinct()
        )
    )
    result["conversas"] = len(conversation_ids)
    campaign = _campaign(db, create=not dry_run)
    seen = _already_in_recall(db, campaign)
    excluded: Counter[str] = Counter()
    by_queue: dict[int, list[Lead]] = {}
    queues: dict[int, ServiceQueue] = {}
    candidates: list[tuple[str, ServiceQueue, Lead]] = []
    blocked_phones: set[str] = set()

    for conversation_id in conversation_ids:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            continue
        lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
        queue_id = db.scalar(
            select(ConversationQueueLink.queue_id).where(ConversationQueueLink.conversation_id == conversation.id)
        )
        queue = db.get(ServiceQueue, queue_id) if queue_id else None
        reason = _exclusion(db, conversation, lead, queue)
        phone = normalize_phone(lead.phone) if lead else ""
        if reason:
            # Um motivo em qualquer conversa do mesmo telefone tira o cliente da lista.
            excluded[reason] += 1
            if phone:
                blocked_phones.add(phone)
            continue
        candidates.append((phone, queue, lead))

    for phone, queue, lead in candidates:
        if phone in blocked_phones:
            continue
        if phone in seen:
            excluded["ja_esta_na_rechamada_ou_repetido"] += 1
            continue
        seen.add(phone)
        queues[queue.id] = queue
        by_queue.setdefault(queue.id, []).append(lead)

    result["elegiveis"] = sum(len(items) for items in by_queue.values())
    result["excluidos"] = dict(excluded)
    result["por_fila"] = {queues[queue_id].name: len(items) for queue_id, items in by_queue.items()}
    if dry_run or not campaign:
        return result

    for queue_id, leads in by_queue.items():
        dispatch, summary = create_dispatch_from_leads(
            db,
            campaign=campaign,
            queue=queues[queue_id],
            leads=leads,
            created_by_id=None,
            interval_seconds=INTERVAL_SECONDS,
            daily_limit=DAILY_LIMIT,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            auto_start=False,
            source="RECALL_IA",
        )
        if dispatch:
            result["disparos"].append(
                {"id": dispatch.id, "fila": queues[queue_id].name, "contatos": summary.get("imported", 0)}
            )
    return result


def run_recall_from_settings(db, mode: str) -> None:
    """Chamado na subida do web. Nunca envia mensagem e nunca impede a subida."""
    mode = (mode or "").strip().lower()
    if mode not in {"dry_run", "prepare"}:
        return
    if db.get_bind().dialect.name == "postgresql":
        # Duas réplicas subindo juntas não podem criar o mesmo rascunho duas vezes.
        if not db.scalar(text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": _LOCK_ID}):
            LOG.info("recall_dispatch skipped=lock")
            return
    result = prepare_recall_dispatches(db, dry_run=mode != "prepare")
    db.commit()
    LOG.info(
        "recall_dispatch mode=%s conversas=%s elegiveis=%s por_fila=%s excluidos=%s disparos=%s",
        mode,
        result["conversas"],
        result["elegiveis"],
        result.get("por_fila", {}),
        result["excluidos"],
        result["disparos"],
    )
