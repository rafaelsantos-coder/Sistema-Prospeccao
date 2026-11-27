from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIFollowupJob,
    Conversation,
    Lead,
    Message,
)
from app.services import dispatch_offer
from app.services.ai_sales_protocol import (
    BASELINE_RULES as SALES_PROTOCOL_RULES,
    coaching_for_context,
    requested_return_not_before,
)
from app.services.ai_queue_scope import is_dedicated_ai_queue
from app.services.queue_routing import queue_for_conversation
from app.services.ai_quick_replies import QuickReply, quick_replies_for_message
from app.services.ai_sdr_policy import SYSTEM_KNOWLEDGE_ITEMS, SULIA_SYSTEM_PROMPT, followup_spec
from app.services.audit import write_audit
from app.services.global_blacklist import block_phone
from app.timeutils import utc_naive, utcnow


LOG = logging.getLogger("sulnet_ai_followups")


class FollowupDecision(BaseModel):
    reply_text: str = Field(min_length=1, max_length=1200)
    quick_replies: list[QuickReply] = Field(default_factory=list, max_length=3)
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


def business_window_start(now: datetime) -> datetime:
    """SulIA funciona 24/7: não adia follow-up por horário ou dia da semana."""
    return utc_naive(now)


# Cadência de oportunidade quente: tempo contado desde a última mensagem enviada.
# O cliente já confirmou compra ou está em uma negociação de alta intenção.
HOT_FOLLOWUP_CADENCE = (
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
)
CLOSING_CADENCE = HOT_FOLLOWUP_CADENCE
MAX_UNANSWERED_CLOSING_FOLLOWUPS = 4
MAX_UNANSWERED_HOT_FOLLOWUPS = 4
CLOSING_FOLLOWUP_GAP = HOT_FOLLOWUP_CADENCE[0]


def closing_window_start(now: datetime) -> datetime:
    """Compatibilidade: também não há restrição de horário no fechamento."""
    return utc_naive(now)


def closing_awaits_customer(db: Session, conversation_id: int) -> bool:
    """O Vendedor IA está numa etapa que depende de resposta do cliente."""
    from app.services.ai_seller import SELLER_CUSTOMER_INPUT_STEPS  # noqa: PLC0415

    state = db.scalar(
        select(AIConversationState.state).where(AIConversationState.conversation_id == conversation_id)
    )
    if state != "CLOSING":
        return False
    step = db.scalar(select(AIClosingState.step).where(AIClosingState.conversation_id == conversation_id))
    return bool(step) and step in SELLER_CUSTOMER_INPUT_STEPS and step not in {"ESCALATED", "COMPLETED"}


def hot_lead_awaits_customer(db: Session, conversation_id: int) -> bool:
    """Somente a negociação ativa do cliente recebe a cadência acelerada."""
    if closing_awaits_customer(db, conversation_id):
        return True
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    return bool(
        state
        and getattr(state, "state", None) == "AI_ACTIVE"
        and (
            str(getattr(state, "temperature", "") or "").upper() == "QUENTE"
            or getattr(state, "last_intent", None) == "PURCHASE_INTENT"
        )
    )


def followup_delay_for(db: Session, conversation_id: int, step: int) -> timedelta:
    """Mesmo cálculo no agendamento normal, no backfill e na recuperação."""
    if hot_lead_awaits_customer(db, conversation_id):
        index = min(max(1, step), len(HOT_FOLLOWUP_CADENCE)) - 1
        return HOT_FOLLOWUP_CADENCE[index]
    delay, _theme = followup_spec(step)
    if step <= 1 and customer_engaged(db, conversation_id):
        return min(delay, ENGAGED_FOLLOWUP_DELAY)
    return delay


def _limit_one_unanswered_per_day(
    db: Session,
    conversation_id: int,
    due: datetime,
) -> datetime:
    """Compatibilidade: sem cota diária; o limite de tentativas permanece ativo."""
    return due


def _outside_fiber_area(db: Session, conversation_id: int) -> bool:
    from app.services.ai_sdr import _conversation_messages
    from app.services.ai_sdr_policies import confirmed_out_of_area_city, _confirmed_facts_from_history

    rows = _conversation_messages(db, conversation_id, limit=160)
    return bool(confirmed_out_of_area_city({"confirmed_facts": _confirmed_facts_from_history(rows)}))


def schedule_followup(
    db: Session,
    *,
    agent_id: int,
    queue_id: int,
    conversation_id: int,
    source_message_id: int | None,
    step: int = 1,
    pending_action: str | None = None,
    pilot_theme: str | None = None,
) -> AIFollowupJob | None:
    from app.services.ai_queue_scope import ai_allowed_for_conversation  # noqa: PLC0415
    if not ai_allowed_for_conversation(db, conversation_id, fallback_queue_id=queue_id):
        return None
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return None
    if _outside_fiber_area(db, conversation_id):
        return None
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    if lead and (lead.do_not_contact or lead.stage in {"GANHO", "NAO_CONTATAR"}):
        return None
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    agent = db.get(AIAgentProfile, agent_id)
    if state and state.state in {"HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "OPT_OUT", "ESCALATED", "COMPLETED"}:
        return None
    if state and state.state == "CLOSING":
        if (
            not agent
            or agent.role_type != "CLOSER"
            or int(state.agent_id or 0) != int(agent_id)
        ):
            return None
    if state and state.state == "AI_ACTIVE":
        if (
            not agent
            or agent.role_type == "CLOSER"
            or int(state.agent_id or 0) != int(agent_id)
        ):
            return None
    closing = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == conversation_id
        )
    )
    if closing and closing.step in {"ESCALATED", "COMPLETED"}:
        return None
    if agent and _customer_declined(db, conversation_id):
        return None
    # Fila pausada: o backfill recriava a cadência a cada ciclo só para ser cancelada.
    if agent and _queue_ai_paused(db, conversation_id):
        return None
    # Antes de criar um novo lembrete, executar os MESMOS limites de envio.
    # Sem isso o backfill recriava dezenas de jobs que seriam cancelados no worker.
    candidate = AIFollowupJob(
        agent_id=agent_id,
        queue_id=queue_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        step=max(1, step),
        status="PENDENTE",
        due_at=utcnow(),
    )
    allowed, reason = _followup_allowed(db, candidate, require_sent_reply=False)
    if not allowed:
        if reason == "CLIENTE_DESISTIU":
            _persist_explicit_no_contact(db, conversation_id)
        return None
    last_terminal = db.scalar(
        select(AIFollowupJob).where(
            AIFollowupJob.conversation_id == conversation_id,
            AIFollowupJob.source_message_id == source_message_id,
            AIFollowupJob.status == "CANCELADO",
            AIFollowupJob.error.in_(
                ("GUARDIAN_OPT_OUT", "CLIENTE_DESISTIU", "LIMITE_SEM_RESPOSTA")
            ),
        ).order_by(AIFollowupJob.id.desc()).limit(1)
    )
    if last_terminal:
        return None

    # A mesma resposta ao disparo recebe apenas uma retomada no piloto,
    # inclusive se o worker tentar reconstruir a cadência depois de reiniciar.
    if source_message_id and db.scalar(select(AIFollowupJob.id).where(
        AIFollowupJob.conversation_id == conversation_id,
        AIFollowupJob.source_message_id == source_message_id,
        AIFollowupJob.theme.in_(dispatch_offer.PILOT_DELAYS),
    ).limit(1)):
        return None

    cancel_pending_followups(db, conversation_id, reason="FOLLOWUP_REAGENDADO")
    delay, theme = followup_spec(step)
    if hot_lead_awaits_customer(db, conversation_id):
        hot_themes = (
            "Retomar a contratação aceita pela etapa pendente, sem pedir novo aceite",
            "Facilitar o dado pendente e esclarecer por que ele é necessário",
            "Ajudar com a dúvida concreta do cadastro, mantendo o plano escolhido",
            "Retomada final cuidadosa, sem pressão, urgência ou concessão inventada",
        )
        theme = hot_themes[min(max(1, step), len(hot_themes)) - 1]
    clean_pending = re.sub(r"\s+", " ", str(pending_action or "")).strip()
    if clean_pending:
        theme = f"{theme}. Pendência atual: {clean_pending[:500]}"
    if pilot_theme in dispatch_offer.PILOT_DELAYS:
        theme = pilot_theme
        due = utcnow() + dispatch_offer.PILOT_DELAYS[theme]
    else:
        delay = followup_delay_for(db, conversation_id, step)
        due = utcnow() + delay
    client_return = _customer_requested_return_time(db, conversation_id)
    if client_return is not None and utc_naive(client_return) > utc_naive(due):
        due = utc_naive(client_return)
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
    # Lembretes do fechamento passam na frente: em 16/11 o da conversa 1382 saiu
    # 23 min atrasado, atrás de cerca de 50 lembretes da prospecção das 9h.
    pending = select(AIFollowupJob).where(
        AIFollowupJob.status == "PENDENTE",
        AIFollowupJob.due_at <= utcnow(),
    )
    job = db.scalar(
        pending.join(AIAgentProfile, AIAgentProfile.id == AIFollowupJob.agent_id)
        .where(AIAgentProfile.role_type == "CLOSER")
        .order_by(AIFollowupJob.due_at.asc(), AIFollowupJob.id.asc())
        .with_for_update(of=AIFollowupJob, skip_locked=True)
        .limit(1)
    ) or db.scalar(
        pending.join(
            AIConversationState,
            AIConversationState.conversation_id == AIFollowupJob.conversation_id,
        )
        .where(
            AIConversationState.state == "AI_ACTIVE",
            AIConversationState.temperature == "QUENTE",
        )
        .order_by(AIFollowupJob.due_at.asc(), AIFollowupJob.id.asc())
        .with_for_update(of=AIFollowupJob, skip_locked=True)
        .limit(1)
    ) or db.scalar(
        pending.order_by(AIFollowupJob.due_at.asc(), AIFollowupJob.id.asc())
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


def _normalize_followup_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _recent_outbound_bodies(history: list[dict], limit: int = 12) -> list[str]:
    values = [
        str(row.get("body") or "").strip()
        for row in history
        if str(row.get("direction") or "").upper() == "OUTBOUND"
        and str(row.get("body") or "").strip()
    ]
    return values[-max(1, limit):]


_FOLLOWUP_STOPWORDS = {
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos",
    "e", "ela", "ele", "em", "essa", "esse", "esta", "este", "eu", "ja",
    "mais", "me", "na", "nas", "no", "nos", "o", "os", "ou", "para",
    "por", "que", "se", "sem", "sua", "seu", "sobre", "um", "uma", "voce",
}


def _followup_content_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize_followup_text(value).split()
        if len(token) >= 4 and token not in _FOLLOWUP_STOPWORDS
    }


def _followup_question(value: str) -> str:
    questions = [
        part.strip()
        for part in re.split(r"[.!]", value or "")
        if "?" in part
    ]
    if not questions:
        return ""
    return _normalize_followup_text(questions[-1].replace("?", " "))


def _followup_exposes_internal_handoff(value: str | None) -> bool:
    folded = _normalize_followup_text(str(value or ""))
    cues = (
        "vendedor ia",
        "vendedor de fechamento",
        "ia de fechamento",
        "vendedor dara continuidade",
        "vendedor continuara",
        "vendedor seguira",
        "encaminhar seu interesse",
    )
    return any(cue in folded for cue in cues)


def _followup_is_repetitive(candidate: str, previous_messages: list[str]) -> bool:
    normalized_candidate = _normalize_followup_text(candidate)
    if not normalized_candidate:
        return True

    candidate_tokens = _followup_content_tokens(candidate)
    candidate_question = _followup_question(candidate)

    for previous in previous_messages:
        normalized_previous = _normalize_followup_text(previous)
        if not normalized_previous:
            continue
        if normalized_candidate == normalized_previous:
            return True

        similarity = SequenceMatcher(
            None,
            normalized_candidate,
            normalized_previous,
        ).ratio()
        if similarity >= 0.70:
            return True

        previous_tokens = _followup_content_tokens(previous)
        if candidate_tokens and previous_tokens:
            containment = len(candidate_tokens & previous_tokens) / min(
                len(candidate_tokens),
                len(previous_tokens),
            )
            if containment >= 0.58:
                return True

        previous_question = _followup_question(previous)
        if candidate_question and previous_question:
            question_similarity = SequenceMatcher(
                None,
                candidate_question,
                previous_question,
            ).ratio()
            if question_similarity >= 0.62:
                return True

    return False


def _unique_followup_fallback(
    *,
    step: int,
    previous_messages: list[str],
) -> FollowupDecision:
    candidates = [
        "Posso te ajudar por outro ângulo: qual ponto é mais importante para você decidir?",
        "Ficou alguma dúvida específica que eu possa esclarecer para facilitar sua decisão?",
        "Quer que eu organize as opções de uma forma mais simples para você comparar?",
        "O que falta para você se sentir seguro para decidir?",
        "Prefere que eu retome pelo valor, pelas características ou pelos benefícios?",
        "Quer que eu te mostre uma alternativa diferente da que vimos até agora?",
        "Posso resumir o que vimos e destacar só o que mais importa para sua escolha?",
        "Tem algum ponto da proposta que ainda não ficou claro para você?",
    ]
    start = max(0, (int(step or 1) - 1) % len(candidates))
    ordered = candidates[start:] + candidates[:start]
    for text in ordered:
        if not _followup_is_repetitive(text, previous_messages):
            return FollowupDecision(
                reply_text=text,
                quick_replies=[],
                rationale="Fallback anti-repetição: mensagem distinta do histórico recente.",
            )
    return FollowupDecision(
        reply_text=f"Posso retomar por um ponto diferente para facilitar sua decisão? Etapa {max(1, int(step or 1))}.",
        quick_replies=[],
        rationale="Fallback final anti-repetição.",
    )


def _pending_action_from_theme(value: str | None) -> str:
    text = str(value or "")
    marker = "Pendência atual:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip()


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


# Desistência explícita do cliente. Só frases inequívocas: "não" sozinho pode ser
# resposta a uma pergunta de sim/não da contratação.
_CUSTOMER_STOP_RE = re.compile(
    r"\b("
    r"nao preciso mais|nao quero mais|nao vou querer|nao vou mais|"
    r"nao (tenho|tem) (mais )?interesse|sem interesse|"
    r"nao quero (nada|internet|esse plano|o plano|contratar)|"
    r"desisti|desisto|desistir|dispenso|"
    r"cancel(a|ar|em|ei|e|ado|ada)|"
    r"par(a|e|em) de (mandar|enviar|me)|nao me (mande|manda|envie|chame|contate)|"
    r"nao entre em contato|me remova|remover meu numero|sair da lista|descadastrar"
    r")\b"
)
# Mensagem inteira (não trecho): "não, obrigado" depois de uma oferta.
_CUSTOMER_STOP_EXACT = {"nao obrigado", "nao obrigada", "prefiro nao", "nao quero"}
# Pedido explícito para NÃO SER CONTATADO é diferente de "não quero este plano".
_NO_CONTACT_RE = re.compile(
    r"\b(?:nao me (?:mande|manda|envie|chame|contate)|"
    r"nao entre em contato|par(?:a|e|em) de (?:mandar|enviar|me)|"
    r"me remova|remover meu numero|retire meu numero da lista|sair da lista|descadastrar|"
    r"nao quero receber (?:mensagens|ofertas)|nao quero mais contato)\b"
)
_GUARDIAN_OPT_OUT_ISSUES = {
    "IGNORES_EXPLICIT_OPT_OUT", "IGNORED_OPT_OUT", "OPT_OUT_IGNORED",
    "CUSTOMER_OPTED_OUT", "CUSTOMER_DECLINED", "DO_NOT_RECONTACT",
    "CLIENT_DECLINED", "CUSTOMER_REFUSAL", "REPEATED_CONTACT_AFTER_REFUSAL",
    "REPEATED_UNWANTED_FOLLOWUP", "UNWANTED_FOLLOWUP",
}


def _persist_explicit_no_contact(db: Session, conversation_id: int) -> bool:
    """Grava opt-out permanente SOMENTE quando o cliente pediu para não contatá-lo."""
    latest = db.scalar(
        select(Message.body).where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        ).order_by(Message.id.desc()).limit(1)
    )
    if not _NO_CONTACT_RE.search(_normalize_followup_text(latest)):
        return False
    conversation = db.get(Conversation, conversation_id)
    if not conversation:
        return False
    # Objetos legados/testes podem não ter relação de contato carregada.
    # A conversa real do banco sempre possui contato; preservamos o opt-out
    # do lead caso o telefone não esteja disponível.
    contact = getattr(conversation, "contact", None)
    phone = getattr(contact, "phone", "") or getattr(getattr(conversation, "lead", None), "phone", "")
    if phone:
        block_phone(
            db, phone=phone,
            name=getattr(contact, "name", ""),
            reason="Cliente solicitou não ser contatado em follow-up",
            source="OPT_OUT_AI",
        )
    if conversation.lead:
        conversation.lead.do_not_contact = True
        conversation.lead.stage = "NAO_CONTATAR"
        conversation.lead.next_action_at = None
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id,
        )
    )
    if state:
        state.state = "OPT_OUT"
        state.last_intent = "DO_NOT_CONTACT"
    conversation.status = "ENCERRADO"
    conversation.closed_at = utcnow()
    cancel_pending_followups(db, conversation_id, reason="OPT_OUT_EXPLICITO")
    LOG.info("ai_opt_out_persisted conversation=%s", conversation_id)
    return True
# Intervalo mínimo entre uma mensagem nossa e um follow-up (pedido do comercial).
FOLLOWUP_MIN_GAP = timedelta(hours=2)
# Conversa Leonardo (10/11): oito mensagens seguidas sem resposta do cliente.
# Auditoria 15/11: 63% das conversas ficaram sem resposta; 2 tentativas bastam, insistir arrisca o número.
MAX_UNANSWERED_FOLLOWUPS = 2


def _latest_inbound_at(db: Session, conversation_id: int):
    return db.scalar(
        select(Message.created_at)
        .where(Message.conversation_id == conversation_id, Message.direction == "INBOUND")
        .order_by(Message.id.desc())
        .limit(1)
    )


def _customer_requested_return_time(db: Session, conversation_id: int) -> datetime | None:
    """Horario combinado somente pela ultima mensagem INBOUND da conversa."""
    latest = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    if latest is None or not latest.body or latest.created_at is None:
        return None
    from app.services.auto_reply import is_automatic_inbound

    if is_automatic_inbound(latest):
        return None
    return requested_return_not_before(latest.body, latest.created_at)


# Cliente que respondeu nas últimas 24 h está engajado: o primeiro follow-up sai em 30 min,
# não em 2 h (conversa Liciane, 10/11). Quem não responde segue a cadência normal.
ENGAGED_WINDOW = timedelta(hours=24)
ENGAGED_FOLLOWUP_DELAY = timedelta(minutes=30)


def customer_engaged(db: Session, conversation_id: int, now: datetime | None = None) -> bool:
    """O cliente escreveu (de verdade, não resposta automática) nas últimas 24 horas."""
    from app.services.auto_reply import is_business_auto_reply  # noqa: PLC0415

    now = now or utcnow()
    base = select(Message).where(
        Message.conversation_id == conversation_id, Message.direction == "INBOUND"
    )
    for message in db.scalars(base.order_by(Message.id.desc()).limit(5)):
        if is_business_auto_reply(message.body):
            continue
        last = utc_naive(message.created_at)
        return bool(last and now - last <= ENGAGED_WINDOW)
    return False


# "Não" seco logo depois de uma oferta ("Quer conhecer a Black?") também é recusa:
# a resposta da IA a ele já foi a tentativa (conversa 651: 3 follow-ups depois do "Não").
_BARE_NEGATIVES = {"nao", "n", "agora nao", "nao quero", "nao obrigado", "nao obrigada", "nao valeu"}
_OFFER_QUESTION_CUES = (
    "quer conhecer",
    "promocao",
    "black",
    "tem interesse",
    "quer contratar",
    "posso te mostrar",
    "quer ver",
    "gostaria de conhecer",
)


def _customer_declined(db: Session, conversation_id: int) -> bool:
    """O cliente recusou: desistência explícita, ou "não" seco a uma oferta."""
    latest_inbound = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    if latest_inbound is None:
        return False
    text = _normalize_followup_text(latest_inbound.body)
    if text in _CUSTOMER_STOP_EXACT or _CUSTOMER_STOP_RE.search(text):
        return True
    if text not in _BARE_NEGATIVES:
        return False
    offer = db.scalar(
        select(Message.body)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "OUTBOUND",
            Message.id < latest_inbound.id,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    offer_text = _normalize_followup_text(offer)
    return any(cue in offer_text for cue in _OFFER_QUESTION_CUES)


def _followup_spacing_hold(
    db: Session, conversation_id: int, now: datetime, *,
    closing: bool = False, pilot_theme: str | None = None, step: int = 1,
) -> datetime | None:
    """Espaçamento mínimo desde a última mensagem, inclusive à noite."""
    from app.services.ai_sdr import human_stepped_in  # noqa: PLC0415
    from app.services.ai_queue_scope import manual_ai_handoff_authorized  # noqa: PLC0415

    conversation = db.get(Conversation, conversation_id)
    if (
        conversation is not None
        and not manual_ai_handoff_authorized(db, conversation_id)
        and not is_dedicated_ai_queue(queue_for_conversation(db, conversation.id))
        and human_stepped_in(db, conversation)
    ):
        # Fora da fila dedicada, preservar o espaçamento após a atuação humana.
        return now + timedelta(minutes=30)
    last_outbound_at = db.scalar(
        select(Message.created_at)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "OUTBOUND",
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    last = utc_naive(last_outbound_at)
    if pilot_theme in dispatch_offer.PILOT_DELAYS:
        gap = dispatch_offer.PILOT_DELAYS[pilot_theme]
    elif closing or hot_lead_awaits_customer(db, conversation_id):
        gap = HOT_FOLLOWUP_CADENCE[min(max(1, step), len(HOT_FOLLOWUP_CADENCE)) - 1]
    else:
        gap = ENGAGED_FOLLOWUP_DELAY if customer_engaged(db, conversation_id, now) else FOLLOWUP_MIN_GAP
    if not last or now - last >= gap:
        return None
    return last + gap


def _queue_ai_paused(db: Session, conversation_id: int) -> bool:
    """A fila da conversa está sem IA ativa (o telefone de teste continua liberado)."""
    from app.services.ai_sdr import binding_for_conversation  # ai_sdr importa este módulo

    return binding_for_conversation(db, conversation_id) is None


def _followup_allowed(
    db: Session, job: AIFollowupJob, *, require_sent_reply: bool = True,
) -> tuple[bool, str]:
    conversation = db.get(Conversation, job.conversation_id)
    if not conversation:
        return False, "CONVERSA_INEXISTENTE"
    if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return False, "CONVERSA_ENCERRADA"
    if _outside_fiber_area(db, conversation.id):
        return False, "CIDADE_SEM_COBERTURA"
    from app.services.global_blacklist import is_globally_blocked  # noqa: PLC0415
    if is_globally_blocked(db, conversation.contact.phone):
        return False, "BLACKLIST_GLOBAL"
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
    agent = db.get(AIAgentProfile, job.agent_id)
    if state and state.state in {"HANDOFF", "HANDOFF_HUMAN", "HUMAN_ACTIVE", "OPT_OUT", "ESCALATED", "COMPLETED"}:
        return False, state.state
    if state and state.state == "CLOSING":
        if (
            not agent
            or agent.role_type != "CLOSER"
            or int(state.agent_id or 0) != int(job.agent_id)
        ):
            return False, "OWNER_CLOSER_AI"
    if state and state.state == "AI_ACTIVE":
        if (
            not agent
            or agent.role_type == "CLOSER"
            or int(state.agent_id or 0) != int(job.agent_id)
        ):
            return False, "OWNER_SDR_AI"
    # O vendedor humano tem prioridade: não disputar a conversa com a IA.
    from app.services.ai_sdr import human_is_attending, human_stepped_in  # noqa: PLC0415
    from app.services.ai_queue_scope import manual_ai_handoff_authorized  # noqa: PLC0415
    if human_is_attending(db, conversation) or (
        not manual_ai_handoff_authorized(db, conversation.id)
        and not is_dedicated_ai_queue(queue_for_conversation(db, conversation.id))
        and human_stepped_in(db, conversation)
    ):
        return False, "ATENDIMENTO_HUMANO_ATIVO"
    # "Pausar IA" na fila: a IA não puxa conversa (nem SDR nem Vendedor IA).
    # O Vendedor IA ainda responde quem escrever e entrega contrato/assinatura.
    if agent is not None and _queue_ai_paused(db, conversation.id):
        return False, "FILA_SEM_IA"
    # Cliente disse que não tem interesse: a resposta da IA a essa mensagem já foi
    # a única tentativa; follow-up não sai (conversa 237: "Qual seu CPF?" 3 vezes).
    if agent is not None and _customer_declined(db, conversation.id):
        return False, "CLIENTE_DESISTIU"
    closing = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == conversation.id
        )
    )
    if closing and closing.step in {"ESCALATED", "COMPLETED"}:
        return False, closing.step
    from app.services.auto_reply import latest_inbound_is_auto_reply  # noqa: PLC0415

    if latest_inbound_is_auto_reply(db, conversation.id):
        return False, "RESPOSTA_AUTOMATICA"
    if require_sent_reply and job.source_message_id:
        from app.services.ai_reply_delivery import reply_not_sent

        if reply_not_sent(db, job.source_message_id):
            return False, "RESPOSTA_AINDA_NAO_ENVIADA"
    sent_stmt = select(AIFollowupJob.id).where(
        AIFollowupJob.conversation_id == conversation.id,
        AIFollowupJob.status == "ENVIADO",
        AIFollowupJob.sent_at.is_not(None),
    )
    last_inbound_at = _latest_inbound_at(db, conversation.id)
    if getattr(job, "theme", None) in dispatch_offer.PILOT_DELAYS:
        if not last_inbound_at or utcnow() - utc_naive(last_inbound_at) >= timedelta(hours=24):
            return False, "JANELA_DO_CLIENTE_ENCERRADA"
    if last_inbound_at:
        sent_stmt = sent_stmt.where(AIFollowupJob.sent_at > utc_naive(last_inbound_at))
    unanswered_sent = len(list(db.scalars(sent_stmt)))
    if closing_awaits_customer(db, conversation.id):
        limit = MAX_UNANSWERED_CLOSING_FOLLOWUPS
    elif hot_lead_awaits_customer(db, conversation.id):
        limit = MAX_UNANSWERED_HOT_FOLLOWUPS
    else:
        limit = MAX_UNANSWERED_FOLLOWUPS
    if unanswered_sent >= limit:
        return False, "LIMITE_SEM_RESPOSTA"
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
    avoid_messages: list[str] | None = None,
) -> FollowupDecision:
    if getattr(job, "theme", None) in dispatch_offer.PILOT_DELAYS:
        return FollowupDecision(reply_text=dispatch_offer.pilot_followup_text(job.theme))
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")

    from agents import Agent
    from app.services.ai_runtime import run_agent_sync

    conversation = db.get(Conversation, job.conversation_id)
    lead = db.get(Lead, conversation.lead_id) if conversation and conversation.lead_id else None
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == job.conversation_id
        )
    )
    from app.services.ai_sdr import _journey_memory_from_lead, retrieve_knowledge

    history = _recent_history(db, job.conversation_id)
    inbound_history = [
        row for row in history
        if str(row.get("direction") or "").upper() == "INBOUND"
    ]
    retrieval_query = " ".join(
        [
            job.theme or "",
            " ".join(str(row.get("body") or "") for row in inbound_history[-8:]),
        ]
    )
    relevant_knowledge = retrieve_knowledge(
        db,
        agent=agent,
        query=retrieval_query,
    )
    if relevant_knowledge:
        knowledge = "\n\n".join(
            f"[{item.category}] {item.title}: {item.content}"
            for item in relevant_knowledge
        )
    else:
        knowledge = "\n\n".join(
            f"[{item['category']}] {item['title']}: {item['content']}"
            for item in SYSTEM_KNOWLEDGE_ITEMS
        )
    previous_outbound = _recent_outbound_bodies(history)
    explicit_avoid = [
        item for item in (avoid_messages or [])
        if str(item or "").strip()
    ]
    forbidden_messages = (previous_outbound + explicit_avoid)[-15:]
    from app.services.ai_learning import learning_context_for_conversation
    learning_experiment = learning_context_for_conversation(
        db,
        conversation_id=job.conversation_id,
        scope="FOLLOWUP_COPY",
    )

    context = {
        "followup_step": job.step,
        "followup_theme": job.theme,
        "customer_engaged": customer_engaged(db, job.conversation_id),
        "forbidden_previous_messages": forbidden_messages,
        "learning_experiment": learning_experiment,
        "lead": {
            "city": lead.city if lead else "",
            "stage": lead.stage if lead else "",
            "journey": _journey_memory_from_lead(
                lead,
                conversation_id=job.conversation_id,
            ),
        },
        "ai_state": {
            "last_intent": state.last_intent if state else "UNKNOWN",
            "temperature": state.temperature if state else "FRIO",
        },
        "customer_evidence": inbound_history,
        "history": history,
        "latest_customer_message": str(inbound_history[-1].get("body") or "") if inbound_history else "",
    }
    prompt = (
        "Crie APENAS a próxima mensagem de follow-up comercial para WhatsApp. "
        "Ela deve retomar a conversa com naturalidade, usar o contexto real, ter um motivo comercial e "
        "estimular resposta. A 'Pendência atual' registrada no objetivo/jornada tem prioridade: o follow-up deve tentar "
        "resolver exatamente essa pendência sem reabrir etapas já concluídas. REGRA DE EVIDÊNCIA: apenas mensagens INBOUND/customer_evidence são falas do cliente. "
        "Mensagens OUTBOUND são falas do SulIA e jamais podem virar fato sobre provedor, preço, velocidade, uso, "
        "satisfação, residência ou empresa. Se um dado não foi confirmado pelo cliente, pergunte; não assuma. "
        "É PROIBIDO repetir qualquer mensagem OUTBOUND anterior, inclusive com pequenas trocas de palavras. "
        "Leia forbidden_previous_messages e produza uma mensagem claramente diferente em estrutura, pergunta e CTA. "
        "Não repita a mesma pergunta, não copie a mesma CTA e não envie variações quase idênticas. "
        "Não aumente desconto por silêncio. Não invente urgência. Não diga 'viu minha mensagem?'. Faça no máximo uma pergunta principal. "
        "Se learning_experiment trouxer instrução, aplique somente ajustes de redação dentro do escopo de follow-up; "
        "é proibido alterar preço, plano, condição comercial, estado da jornada, contrato, crédito ou regra de handoff. "
        "Em oportunidade quente, a cadência é 10 minutos, depois 30 minutos, 2 horas e 12 horas; "
        "cada tentativa deve usar um ângulo comercial diferente e sem pressão excessiva. "
        "Quando essa pergunta tiver 2 ou 3 respostas previsíveis, retorne quick_replies com botões curtos "
        "(máximo 20 caracteres). Se a resposta exigir texto livre, quick_replies pode ficar vazio. "
        "COMO ATRAIR O CLIENTE DE VOLTA: se customer_engaged=true, ele respondeu há pouco e parou no meio; "
        "continue exatamente de onde parou, com algo NOVO e útil (confirmar cobertura, dizer o valor em reais, "
        "o prazo de ativação ou o próximo passo concreto), chamando pelo primeiro nome quando souber. "
        "Faça o próximo passo parecer fácil: uma única pergunta de resposta curta, com botões quando couber. "
        "Tom de quem está ajudando, nunca de cobrança: sem 'e aí?', sem 'ainda tem interesse?', sem pedir desculpa por insistir. "
        "Se a última pergunta nossa ficou sem resposta, não a repita: ofereça um caminho mais simples para responder. "
        "Se a base não sustentar uma afirmação, não a use.\n\n"
        f"OBJETIVO DESTA ETAPA: {job.theme}\n\n"
        f"CONTEXTO: {json.dumps(context, ensure_ascii=False, default=str)}\n\n"
        f"BASE OFICIAL: {knowledge}"
    )
    followup_agent = Agent(
        name=f"{agent.name} Follow-up",
        instructions=(
            ((agent.system_prompt or "").strip() or SULIA_SYSTEM_PROMPT)
            + "\n\n" + SALES_PROTOCOL_RULES
            + "\n" + coaching_for_context(context)
        ),
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 45.0,
        },
        output_type=FollowupDecision,
    )
    result = run_agent_sync(followup_agent, prompt)
    value = result.final_output
    if not isinstance(value, FollowupDecision):
        value = FollowupDecision.model_validate(value)
    return value


def _zapi_instance_offline(exc: Exception) -> bool:
    """Z-API recusou o envio porque o WhatsApp da instância está desconectado."""
    return "whatsapp is disconnected" in str(exc).lower()


def process_followup_job(db: Session, job: AIFollowupJob) -> str:
    from app.services.ai_queue_scope import ai_allowed_for_conversation  # noqa: PLC0415
    if not ai_allowed_for_conversation(db, job.conversation_id, fallback_queue_id=job.queue_id):
        job.status = "CANCELADO"
        job.error = "IA autorizada somente na Fila IA."
        return "cancelled"
    from app.services.ai_sdr import is_human_only_queue  # noqa: PLC0415
    from app.services.queue_routing import queue_for_conversation  # noqa: PLC0415
    if is_human_only_queue(queue_for_conversation(db, job.conversation_id)):
        job.status = "CANCELADO"
        job.error = "Fila Natine exclusiva de atendimento humano."
        return "cancelled"
    allowed, reason = _followup_allowed(db, job)
    if not allowed:
        job.status = "CANCELADO"
        job.error = reason
        if reason == "CLIENTE_DESISTIU":
            _persist_explicit_no_contact(db, job.conversation_id)
        return "cancelled"

    # A IA responde 24/7, mas nao antecipa a data combinada pelo cliente.
    now = utcnow()
    client_return = _customer_requested_return_time(db, job.conversation_id)
    if client_return is not None and utc_naive(client_return) > utc_naive(now):
        job.status = "PENDENTE"
        job.due_at = utc_naive(client_return)
        job.locked_at = None
        job.attempts = max(0, int(job.attempts or 0) - 1)
        return "customer_schedule"
    closing = closing_awaits_customer(db, job.conversation_id)

    pilot = getattr(job, "theme", None) in dispatch_offer.PILOT_DELAYS
    spacing_options = {"pilot_theme": job.theme} if pilot else {}
    hold_until = _followup_spacing_hold(db, job.conversation_id, now, closing=closing, step=int(job.step or 1), **spacing_options)
    if hold_until and hold_until > now:
        job.status = "PENDENTE"
        job.due_at = hold_until
        job.locked_at = None
        job.attempts = max(0, int(job.attempts or 0) - 1)
        return "spacing"

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

    if agent.role_type == "CLOSER":
        from app.services.ai_seller import process_seller_followup
        return process_seller_followup(db, job, agent)

    try:
        history = _recent_history(db, job.conversation_id, limit=40)
        previous_outbound = _recent_outbound_bodies(history)
        decision = _generate_followup(db, agent=agent, job=job)

        if pilot and decision.reply_text in previous_outbound:
            job.status = "CANCELADO"
            job.error = "PILOTO_MENSAGEM_JA_ENVIADA"
            return "cancelled"
        if _followup_exposes_internal_handoff(decision.reply_text):
            first_attempt = decision.reply_text
            decision = _generate_followup(
                db,
                agent=agent,
                job=job,
                avoid_messages=previous_outbound + [first_attempt],
            )
            if _followup_exposes_internal_handoff(decision.reply_text):
                decision = _unique_followup_fallback(
                    step=job.step,
                    previous_messages=previous_outbound + [first_attempt],
                )

        if not pilot and _followup_is_repetitive(decision.reply_text, previous_outbound):
            first_attempt = decision.reply_text
            decision = _generate_followup(
                db,
                agent=agent,
                job=job,
                avoid_messages=previous_outbound + [first_attempt],
            )

        if not pilot and _followup_is_repetitive(decision.reply_text, previous_outbound):
            decision = _unique_followup_fallback(
                step=job.step,
                previous_messages=previous_outbound,
            )

        # O cliente pode ter respondido enquanto o modelo gerava a mensagem.
        # Revalida imediatamente antes do envio para não atropelar a resposta.
        allowed_now, reason_now = _followup_allowed(db, job)
        if not allowed_now:
            job.status = "CANCELADO"
            job.error = reason_now
            return "cancelled"

        conversation = db.get(Conversation, job.conversation_id)
        if not conversation or not conversation.contact or not conversation.contact.phone:
            raise RuntimeError("Conversa sem telefone para follow-up.")

        from app.services.ai_guardian import guard_outbound_message

        guardian = guard_outbound_message(
            db,
            agent=agent,
            conversation=conversation,
            reply_text=decision.reply_text,
            buttons=[item.model_dump() for item in decision.quick_replies],
            source="SDR_FOLLOWUP",
            proposed_state="AI_ACTIVE",
            metadata={"followup_step": job.step, "theme": job.theme},
        )
        if _GUARDIAN_OPT_OUT_ISSUES.intersection(guardian.issue_codes):
            # Regra bloqueante: jamais transformar recusa em nova proposta.
            job.status = "CANCELADO"
            job.error = "GUARDIAN_OPT_OUT"
            _persist_explicit_no_contact(db, job.conversation_id)
            LOG.warning("ai_followup_opt_out_cancelled conversation=%s job=%s", job.conversation_id, job.id)
            return "cancelled"
        if not guardian.should_send:
            first_blocked = decision.reply_text
            decision = _generate_followup(
                db,
                agent=agent,
                job=job,
                avoid_messages=previous_outbound + [first_blocked],
            )
            guardian = guard_outbound_message(
                db,
                agent=agent,
                conversation=conversation,
                reply_text=decision.reply_text,
                buttons=[item.model_dump() for item in decision.quick_replies],
                source="SDR_FOLLOWUP_RETRY",
                proposed_state="AI_ACTIVE",
                metadata={"followup_step": job.step, "theme": job.theme},
            )

        if _GUARDIAN_OPT_OUT_ISSUES.intersection(guardian.issue_codes):
            job.status = "CANCELADO"
            job.error = "GUARDIAN_OPT_OUT"
            _persist_explicit_no_contact(db, job.conversation_id)
            return "cancelled"

        if not guardian.should_send:
            job.status = "PENDENTE"
            job.locked_at = None
            job.error = (
                "Guardião bloqueou follow-up: "
                + (", ".join(guardian.issue_codes) or guardian.rationale)
            )[:2000]
            job.due_at = utcnow() + timedelta(minutes=30)
            return "guardian_blocked"

        if guardian.verdict in {"REWRITE", "CORRECT_STATE"}:
            decision.reply_text = guardian.reply_text
            decision.quick_replies = [
                QuickReply.model_validate(item)
                for item in guardian.buttons
            ]

        # Guardião pode reescrever após uma checagem anterior; verificar de novo
        # se o cliente respondeu, desistiu ou alterou o dono da conversa.
        allowed_after_guard, stop_reason = _followup_allowed(db, job)
        if not allowed_after_guard:
            job.status = "CANCELADO"
            job.error = stop_reason
            if stop_reason == "CLIENTE_DESISTIU":
                _persist_explicit_no_contact(db, job.conversation_id)
            return "cancelled"

        if _followup_exposes_internal_handoff(decision.reply_text):
            job.status = "CANCELADO"
            job.error = "SDR_FOLLOWUP_INTERNAL_HANDOFF_BLOCKED"
            LOG.warning(
                "sdr_followup_internal_handoff_blocked conversation=%s job=%s",
                job.conversation_id,
                job.id,
            )
            return "cancelled"

        from app.services.conversation_engine import register_message
        from app.services.zapi_instances import service_for_conversation

        if not ai_allowed_for_conversation(db, conversation.id, fallback_queue_id=job.queue_id):
            job.status = "CANCELADO"
            job.error = "Atendimento humano ou IA nao autorizada."
            return "cancelled"
        zapi = service_for_conversation(db, conversation.id)
        quick_replies = quick_replies_for_message(
            decision.reply_text,
            decision.quick_replies,
        )
        if quick_replies:
            result = zapi.send_button_list(
                phone=conversation.contact.phone,
                message=decision.reply_text,
                buttons=quick_replies,
            )
            message_type = "BUTTONS"
        else:
            result = zapi.send_text(
                phone=conversation.contact.phone,
                message=decision.reply_text,
            )
            message_type = "TEXT"
        provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
        zaap_id = str(result.get("zaapId") or "").strip() or None
        message = register_message(
            db,
            conversation,
            direction="OUTBOUND",
            message_type=message_type,
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
                "quick_reply_count": len(quick_replies),
            },
        )

        if pilot:
            write_audit(db, user=agent.user, action="DISPATCH_PILOT_REMINDER",
                        entity_type="message", entity_id=message.id,
                        payload={"source_message_id": job.source_message_id})
            return "sent"

        schedule_followup(
            db,
            agent_id=job.agent_id,
            queue_id=job.queue_id,
            conversation_id=job.conversation_id,
            source_message_id=job.source_message_id,
            step=job.step + 1,
            pending_action=_pending_action_from_theme(job.theme),
        )
        return "sent"
    except Exception as exc:
        job.error = str(exc)[:2000]
        if _zapi_instance_offline(exc):
            # Instância do WhatsApp desconectada: não é falha do follow-up.
            # Não consome tentativa (senão 3 tentativas em 15 min viram FALHOU e o
            # lead esfria); volta para a fila e tenta de novo quando reconectar.
            job.status = "PENDENTE"
            job.locked_at = None
            job.attempts = max(0, int(job.attempts or 0) - 1)
            job.due_at = utcnow() + timedelta(minutes=15)
            LOG.warning(
                "followup_zapi_offline_requeued job=%s conversation=%s",
                job.id,
                job.conversation_id,
            )
            return "zapi_offline"
        if int(job.attempts or 0) < 3:
            job.status = "PENDENTE"
            job.locked_at = None
            job.due_at = utcnow() + timedelta(minutes=5 * int(job.attempts or 1))
        else:
            job.status = "FALHOU"
        LOG.exception("Falha no follow-up IA job=%s", job.id)
        return "failed"
