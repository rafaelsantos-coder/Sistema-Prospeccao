"""Monta o disparo da rechamada (ai_campaign_recall) para quem a IA atendeu.

O disparo nasce em RASCUNHO, um por fila, para o gestor conferir e iniciar na
tela de Disparos. Controlado por RECALL_IA_DISPATCH: "dry_run" só registra as
contagens no log; "prepare" cria os rascunhos; "start" cria e já inicia o envio
(autorizado pelo gestor em 15/11), no intervalo e na janela definidos abaixo.
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
    Contact,
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


def _history(db, conversation_id: int) -> list[dict]:
    rows = db.execute(
        select(Message.direction, Message.body)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    ).all()
    return [{"direction": direction, "body": body} for direction, body in rows]


def _lead_for(db, conversation: Conversation, contact: Contact | None, phone: str) -> Lead | None:
    """Lead da conversa. Quem escreveu primeiro costuma ter só o contato, sem lead."""
    for lead_id in (conversation.lead_id, contact.lead_id if contact else None):
        lead = db.get(Lead, lead_id) if lead_id else None
        if lead:
            return lead
    if not phone:
        return None
    return db.scalar(
        select(Lead).where(Lead.phone == phone).order_by(Lead.updated_at.desc(), Lead.id.desc()).limit(1)
    )


def _create_lead(db, conversation: Conversation, contact: Contact, phone: str, queue: ServiceQueue) -> Lead:
    name = (contact.name or contact.business_name or "").strip()
    lead = Lead(
        business_name=name or phone,
        contact_name=name,
        phone=phone,
        segment="Não informado",
        # Cidade desconhecida de propósito: a IA pergunta antes de fechar o 800 Mega.
        city="Não informado",
        source="IMPORTACAO",
        origin_channel="WHATSAPP",
        team=queue.team,
        stage="CONTATO_REALIZADO",
        score=0,
        temperature="FRIO",
        priority="NORMAL",
        branch_id=queue.branch_id,
        notes="Lead criada a partir de conversa atendida pela IA (rechamada).",
        raw_data=json.dumps({"import_source": "recall_ia"}, ensure_ascii=False),
    )
    db.add(lead)
    db.flush()
    if not contact.lead_id:
        contact.lead_id = lead.id
    if not conversation.lead_id:
        conversation.lead_id = lead.id
    return lead


def _exclusion(
    db,
    conversation: Conversation,
    lead: Lead | None,
    phone: str,
    queue: ServiceQueue | None,
) -> str:
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
    from app.services.ai_sdr_policies import _existing_customer_from_context  # noqa: PLC0415

    if not phone:
        return "sem_telefone"
    if lead:
        if lead.do_not_contact or lead.stage == "NAO_CONTATAR":
            return "nao_contatar"
        if lead.stage == "GANHO":
            return "ja_fechou"
        if lead.stage == "PERDIDO":
            return "perdido"
    if conversation.status == "BLOQUEADO":
        return "conversa_bloqueada"
    if (
        _confirmed_existing_customer_from_lead(lead) == "CLIENTE"
        or _existing_customer_from_context({"history": _history(db, conversation.id)}) == "CLIENTE"
    ):
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
    city = _service_city_in_text(lead.city or "") if lead else ""
    plan = _ftth_plan_by_speed(recall.PLAN_SPEED)
    if city and plan and not _plan_available_in_city(plan, city):
        return "cidade_sem_800_mega"
    return ""


def prepare_recall_dispatches(db, *, dry_run: bool = True, auto_start: bool = False) -> dict:
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
    candidates: list[tuple[str, ServiceQueue, Lead | None, Conversation, Contact | None]] = []
    counts_only: dict[int, int] = {}
    blocked_phones: set[str] = set()

    for conversation_id in conversation_ids:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            continue
        contact = db.get(Contact, conversation.contact_id) if conversation.contact_id else None
        phone = normalize_phone(contact.phone if contact else None) or ""
        lead = _lead_for(db, conversation, contact, phone)
        if not phone and lead:
            phone = normalize_phone(lead.phone) or ""
        queue_id = db.scalar(
            select(ConversationQueueLink.queue_id).where(ConversationQueueLink.conversation_id == conversation.id)
        )
        queue = db.get(ServiceQueue, queue_id) if queue_id else None
        reason = _exclusion(db, conversation, lead, phone, queue)
        if reason:
            # Um motivo em qualquer conversa do mesmo telefone tira o cliente da lista.
            excluded[reason] += 1
            if phone:
                blocked_phones.add(phone)
            continue
        candidates.append((phone, queue, lead, conversation, contact))

    without_lead = 0
    for phone, queue, lead, conversation, contact in candidates:
        if phone in blocked_phones:
            continue
        if phone in seen:
            excluded["ja_esta_na_rechamada_ou_repetido"] += 1
            continue
        seen.add(phone)
        if not lead:
            without_lead += 1
            if dry_run or not contact:
                # No dry_run só conta; o lead é criado quando o disparo é montado.
                by_queue.setdefault(queue.id, [])
                queues[queue.id] = queue
                counts_only[queue.id] = counts_only.get(queue.id, 0) + 1
                continue
            lead = _create_lead(db, conversation, contact, phone, queue)
        elif not normalize_phone(lead.phone):
            lead.phone = phone
        queues[queue.id] = queue
        by_queue.setdefault(queue.id, []).append(lead)

    result["elegiveis"] = sum(len(items) for items in by_queue.values()) + sum(counts_only.values())
    result["sem_lead"] = without_lead
    result["excluidos"] = dict(excluded)
    result["por_fila"] = {
        queues[queue_id].name: len(items) + counts_only.get(queue_id, 0) for queue_id, items in by_queue.items()
    }
    if dry_run or not campaign:
        return result

    for queue_id, leads in by_queue.items():
        if not leads:
            continue
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
            auto_start=auto_start,
            source="RECALL_IA",
        )
        if dispatch:
            result["disparos"].append(
                {"id": dispatch.id, "fila": queues[queue_id].name, "contatos": summary.get("imported", 0)}
            )
    return result


# --- Rechamada ampliada (16/11) -------------------------------------------
# Todo cliente que falou com a empresa nos últimos 7 dias, de qualquer fila, e
# que ainda não recebeu a rechamada. Regras do gestor:
# - vendedor escreveu nos últimos 3 dias: fica de fora (o vendedor está atendendo);
# - negociação aberta: fica com o vendedor, não recebe a IA (lista por vendedor);
# - os demais recebem a IA pela fila com IA, onde o roteiro da rechamada responde.
WIDE_SELLER_QUIET_DAYS = 3
_OPEN_DEAL_STAGES = {"PROPOSTA", "NEGOCIACAO"}


def _seller_messages(db, conversation_id: int, ai_user_ids: set[int]) -> list[tuple[int, object]]:
    """(vendedor, data) das mensagens escritas por uma pessoa; disparos não contam."""
    from app.models import CampaignRecipient  # noqa: PLC0415

    automated = {
        (body or "").strip()
        for model in (CampaignRecipient, CampaignDispatchRecipient)
        for body in db.scalars(select(model.message_body).where(model.conversation_id == conversation_id))
    }
    rows = db.execute(
        select(Message.sender_user_id, Message.created_at, Message.body).where(
            Message.conversation_id == conversation_id,
            Message.direction == "OUTBOUND",
            Message.sender_user_id.is_not(None),
            Message.message_type.in_(("TEXT", "BUTTONS")),
        )
    ).all()
    return [
        (user_id, created_at)
        for user_id, created_at, body in rows
        if user_id not in ai_user_ids and (body or "").strip() and (body or "").strip() not in automated
    ]


def _open_deal(db, conversation: Conversation, lead: Lead | None) -> bool:
    from app.models import AIClosingState  # noqa: PLC0415

    if lead and lead.stage in _OPEN_DEAL_STAGES:
        return True
    return (
        db.scalar(
            select(AIClosingState.id)
            .where(
                AIClosingState.conversation_id == conversation.id,
                AIClosingState.completed_at.is_(None),
            )
            .limit(1)
        )
        is not None
    )


def _ai_target_queue(db, own_queue: ServiceQueue | None) -> ServiceQueue | None:
    """Fila com IA que recebe a resposta: a da conversa, se tiver IA; senão a Fila IA."""
    from app.models import AIQueueBinding  # noqa: PLC0415
    from app.services.ai_sdr import queue_ai_binding  # noqa: PLC0415

    if own_queue and own_queue.active and queue_ai_binding(db, own_queue.id):
        return own_queue
    queues = list(
        db.scalars(
            select(ServiceQueue)
            .join(AIQueueBinding, AIQueueBinding.queue_id == ServiceQueue.id)
            .where(ServiceQueue.active.is_(True), AIQueueBinding.active.is_(True))
            .order_by(ServiceQueue.id)
        )
    )
    named = [queue for queue in queues if "ia" in recall.fold(queue.name).split()]
    return (named or queues or [None])[0]


def prepare_wide_recall(db, *, dry_run: bool = True, auto_start: bool = False) -> dict:
    from app.models import User  # noqa: PLC0415
    from app.services.dispatch_queue import create_dispatch_from_leads  # noqa: PLC0415

    now = utcnow()
    since = now - timedelta(days=DAYS)
    quiet_since = now - timedelta(days=WIDE_SELLER_QUIET_DAYS)
    ai_user_ids = {row for row in db.scalars(select(AIAgentProfile.user_id)) if row}
    conversation_ids = list(
        db.scalars(
            select(Message.conversation_id)
            .where(Message.direction == "INBOUND", Message.created_at >= since)
            .distinct()
        )
    )
    campaign = _campaign(db, create=not dry_run)
    seen = _already_in_recall(db, campaign)
    excluded: Counter[str] = Counter()
    seller_deals: Counter[str] = Counter()
    blocked: set[str] = set()
    candidates: list[tuple[str, ServiceQueue, Lead | None, Conversation, Contact | None, bool]] = []
    sources: Counter[str] = Counter()

    for conversation_id in conversation_ids:
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.source == "TELEGRAM":
            continue
        contact = db.get(Contact, conversation.contact_id) if conversation.contact_id else None
        phone = normalize_phone(contact.phone if contact else None) or ""
        lead = _lead_for(db, conversation, contact, phone)
        if not phone and lead:
            phone = normalize_phone(lead.phone) or ""
        if phone in seen:
            excluded["ja_recebeu_rechamada"] += 1
            blocked.add(phone)
            continue
        own_queue = db.get(
            ServiceQueue,
            db.scalar(
                select(ConversationQueueLink.queue_id).where(
                    ConversationQueueLink.conversation_id == conversation.id
                )
            )
            or 0,
        )
        target = _ai_target_queue(db, own_queue)
        reason = _exclusion(db, conversation, lead, phone, target)
        if reason in {"vendedor_humano_atendendo", "em_fechamento_ou_fora_da_ia"}:
            ai_state = db.scalar(
                select(AIConversationState.state).where(AIConversationState.conversation_id == conversation.id)
            )
            # Quem pediu para sair ou já concluiu continua de fora; o resto passa
            # pela regra dos 3 dias e da negociação aberta logo abaixo.
            reason = "pediu_para_sair_ou_concluido" if ai_state in {"OPT_OUT", "COMPLETED"} else ""
        sellers = _seller_messages(db, conversation.id, ai_user_ids)
        if not reason and any(created_at and created_at >= quiet_since for _, created_at in sellers):
            reason = "vendedor_falou_ultimos_3_dias"
        if not reason and _open_deal(db, conversation, lead):
            reason = "negociacao_aberta_fica_com_vendedor"
            owner_id = conversation.assigned_user_id or (lead.assigned_user_id if lead else None)
            if owner_id in ai_user_ids or not owner_id:
                owner_id = max(sellers, key=lambda item: item[1])[0] if sellers else None
            owner = db.get(User, owner_id) if owner_id else None
            seller_deals[owner.name if owner else "sem vendedor"] += 1
        if reason:
            excluded[reason] += 1
            if phone:
                blocked.add(phone)
            continue
        candidates.append((phone, target, lead, conversation, contact, bool(sellers)))

    by_queue: dict[int, list[Lead]] = {}
    queues: dict[int, ServiceQueue] = {}
    counts: Counter[str] = Counter()
    for phone, queue, lead, conversation, contact, had_seller in candidates:
        if phone in blocked:
            continue
        if phone in seen:
            excluded["telefone_repetido"] += 1
            continue
        seen.add(phone)
        counts[queue.name] += 1
        sources["ja_falou_com_vendedor" if had_seller else "so_ia_ou_sem_atendimento"] += 1
        if dry_run:
            continue
        if not lead:
            if not contact:
                continue
            lead = _create_lead(db, conversation, contact, phone, queue)
        elif not normalize_phone(lead.phone):
            lead.phone = phone
        queues[queue.id] = queue
        by_queue.setdefault(queue.id, []).append(lead)

    result: dict = {
        "dry_run": dry_run,
        "conversas": len(conversation_ids),
        "elegiveis_ia": sum(counts.values()),
        "por_fila_destino": dict(counts),
        "origem": dict(sources),
        "negociacao_por_vendedor": dict(seller_deals),
        "excluidos": dict(excluded),
        "disparos": [],
    }
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
            auto_start=auto_start,
            source="RECALL_IA_AMPLA",
        )
        if dispatch:
            result["disparos"].append(
                {"id": dispatch.id, "fila": queues[queue_id].name, "contatos": summary.get("imported", 0)}
            )
    return result


def run_recall_from_settings(db, mode: str) -> None:
    """Chamado na subida do web. Só o modo "start" inicia o envio; nunca impede a subida."""
    mode = (mode or "").strip().lower()
    if mode not in {"dry_run", "prepare", "start", "wide_dry_run", "wide_prepare", "wide_start"}:
        return
    if db.get_bind().dialect.name == "postgresql":
        # Duas réplicas subindo juntas não podem criar o mesmo rascunho duas vezes.
        if not db.scalar(text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": _LOCK_ID}):
            LOG.info("recall_dispatch skipped=lock")
            return
    if mode.startswith("wide_"):
        wide = prepare_wide_recall(db, dry_run=mode == "wide_dry_run", auto_start=mode == "wide_start")
        db.commit()
        LOG.info(
            "recall_dispatch_ampla mode=%s conversas=%s elegiveis_ia=%s por_fila_destino=%s origem=%s "
            "negociacao_por_vendedor=%s excluidos=%s disparos=%s",
            mode,
            wide["conversas"],
            wide["elegiveis_ia"],
            wide["por_fila_destino"],
            wide["origem"],
            wide["negociacao_por_vendedor"],
            wide["excluidos"],
            wide["disparos"],
        )
        return
    result = prepare_recall_dispatches(db, dry_run=mode == "dry_run", auto_start=mode == "start")
    db.commit()
    LOG.info(
        "recall_dispatch mode=%s conversas=%s elegiveis=%s sem_lead=%s por_fila=%s excluidos=%s disparos=%s",
        mode,
        result["conversas"],
        result["elegiveis"],
        result.get("sem_lead", 0),
        result.get("por_fila", {}),
        result["excluidos"],
        result["disparos"],
    )
