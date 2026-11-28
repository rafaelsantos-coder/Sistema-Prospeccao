"""Avisos operacionais por WhatsApp para o responsável do sistema.

Eventos cobertos:
- instância Z-API desconectou ou reconectou;
- venda finalizada pela IA (assinada e confirmada como ganha);
- conversa escalada para atendimento humano;
- venda parada no fechamento por erro (ex.: plano não encontrado no CRM), para
  um vendedor chamar o cliente antes de perder a venda.

O aviso sai por qualquer instância Z-API ativa e conectada (nunca pela que
caiu). Cada aviso tem chave de deduplicação gravada na auditoria, então o
mesmo evento não é enviado duas vezes. Só os avisos em que um vendedor precisa
chamar o cliente (venda parada, crédito com restrição) levam nome e telefone;
os demais levam identificadores técnicos, plano e valor. CPF nunca é enviado.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIClosingState, AIConversationState, AIJob, AuditLog, Contact, Conversation, Message, ZApiInstance
from app.services.ai_pending_scope import UNREAD_RECOVERY_SINCE_UTC, in_unread_recovery_window
from app.services.audit import write_audit
from app.services.zapi import ZApiError
from app.services.zapi_instances import list_zapi_instances, service_for_instance
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ops_alerts")

ALERT_ACTION = "OPS_ALERT"
STATE_ACTION = "OPS_INSTANCE_STATE"


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _already_sent(db: Session, key: str, cooldown: timedelta | None) -> bool:
    stmt = select(AuditLog.id).where(
        AuditLog.action == ALERT_ACTION,
        AuditLog.entity_id == key,
    )
    if cooldown is not None:
        stmt = stmt.where(AuditLog.created_at >= utcnow() - cooldown)
    return db.scalar(stmt.limit(1)) is not None


def _connected(instance: ZApiInstance) -> bool:
    try:
        return bool(service_for_instance(instance).status().get("connected"))
    except Exception:  # noqa: BLE001 - instância fora do ar conta como desconectada
        return False


_URGENT_INSTALL = re.compile(
    r"24 ?h(oras)?\b|para amanha|pra amanha|ate amanha|hoje ainda|ainda hoje|urgente|urgencia|"
    r"o quanto antes|o mais rapido|mais rapido possivel"
)


def _asked_urgent_install(db: Session, conversation_id: int | None) -> bool:
    """O cliente pediu ativação com urgência em alguma mensagem da conversa."""
    if not conversation_id:
        return False
    bodies = db.scalars(
        select(Message.body).where(
            Message.conversation_id == conversation_id,
            Message.direction == "INBOUND",
        )
    )
    for body in bodies:
        folded = (
            unicodedata.normalize("NFKD", str(body or "")).encode("ascii", "ignore").decode().lower()
        )
        if _URGENT_INSTALL.search(folded):
            return True
    return False


def send_ops_alert(
    db: Session,
    *,
    key: str,
    text: str,
    exclude_instance_ids: set[int] | None = None,
    cooldown: timedelta | None = None,
    recipient_phone: str | None = None,
) -> bool:
    """Envia o aviso uma vez por chave. Destinatario explicito so por chamada autorizada."""
    phone = _digits(recipient_phone if recipient_phone is not None else settings.ops_alert_phone)
    if not phone:
        return False
    if _already_sent(db, key, cooldown):
        return False
    skip = exclude_instance_ids or set()
    for instance in list_zapi_instances(db):
        if instance.id in skip or not _connected(instance):
            continue
        try:
            service_for_instance(instance).send_text(phone=phone, message=text)
        except ZApiError as exc:
            LOG.warning("ops_alert_send_failed instance=%s error=%s", instance.id, exc)
            continue
        write_audit(
            db,
            user=None,
            action=ALERT_ACTION,
            entity_type="ops_alert",
            entity_id=key,
            payload={"via_instance_id": instance.id},
        )
        db.commit()
        LOG.info("ops_alert_sent key=%s via_instance=%s", key, instance.id)
        return True
    LOG.warning("ops_alert_no_connected_instance key=%s", key)
    return False


def _last_state(db: Session, instance_id: int) -> bool | None:
    row = db.scalar(
        select(AuditLog)
        .where(AuditLog.action == STATE_ACTION, AuditLog.entity_id == str(instance_id))
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    if not row:
        return None
    try:
        return bool(json.loads(row.payload_json or "{}").get("connected"))
    except ValueError:
        return None


def check_instances(db: Session) -> int:
    """Avisa quando uma instância muda entre conectada e desconectada."""
    sent = 0
    instances = list_zapi_instances(db)
    for instance in instances:
        now_connected = _connected(instance)
        before = _last_state(db, instance.id)
        if before is None or before != now_connected:
            write_audit(
                db,
                user=None,
                action=STATE_ACTION,
                entity_type="zapi_instance",
                entity_id=instance.id,
                payload={"connected": now_connected},
            )
            db.commit()
        if before is None or before == now_connected:
            continue
        stamp = utcnow().strftime("%Y%m%d%H%M")
        if now_connected:
            pending_note = ""
            try:
                summary = service_for_instance(instance).queue_pending_summary(max_pages=2)
                pending = int(summary.get("count") or 0)
                if pending:
                    suffix = "+" if summary.get("capped") else ""
                    pending_note = (
                        f" Há {pending}{suffix} mensagem(ns) na fila nativa da Z-API "
                        "(serão enviadas automaticamente; limpe em Configurações se estiverem velhas)."
                    )
            except Exception:  # noqa: BLE001 - alerta de conexao nao depende da fila
                pending_note = ""
            text = (
                f"✅ Sulnet Prospecta: o WhatsApp \"{instance.name}\" voltou a ficar conectado."
                f"{pending_note}"
            )
        else:
            text = (
                f"⚠️ Sulnet Prospecta: o WhatsApp \"{instance.name}\" DESCONECTOU. "
                "Atendimentos e disparos por esse número estão parados até reconectar no painel da Z-API. "
                "Antes de religar, revise a fila nativa da Z-API em Configurações → Números WhatsApp."
            )
        if send_ops_alert(
            db,
            key=f"instance:{instance.id}:{int(now_connected)}:{stamp}",
            text=text,
            exclude_instance_ids={instance.id},
        ):
            sent += 1
    return sent


def check_sales_and_escalations(db: Session) -> int:
    """Avisa vendas finalizadas e escaladas nas últimas 24 horas."""
    sent = 0
    since = max(utcnow() - timedelta(hours=24), UNREAD_RECOVERY_SINCE_UTC)
    states = db.scalars(
        select(AIClosingState)
        .where(
            AIClosingState.completed_at.is_not(None),
            AIClosingState.completed_at >= since,
        )
        .order_by(AIClosingState.completed_at.asc())
    )
    for state in states:
        try:
            collected = json.loads(state.collected_json or "{}")
        except ValueError:
            collected = {}
        if state.step != "COMPLETED" or not collected.get("_won_after_signature"):
            continue
        plan = state.plan_label or "plano não informado"
        price = f" · R$ {state.offer_price}" if state.offer_price is not None else ""
        protocol = f" · protocolo {state.protocol}" if state.protocol else ""
        text = (
            f"🎉 Venda finalizada pela IA! Conversa {state.conversation_id}: {plan}{price}{protocol}."
        )
        if _asked_urgent_install(db, state.conversation_id):
            text += " ⚡ Cliente pediu ativação em 24 horas: priorizar a agenda."
        if send_ops_alert(db, key=f"sale:{state.id}", text=text):
            sent += 1

    escalated = db.scalars(
        select(AIClosingState).where(
            AIClosingState.step == "ESCALATED",
            AIClosingState.updated_at >= since,
        )
    )
    for state in escalated:
        text = (
            f"🙋 Atendimento escalado para humano: conversa {state.conversation_id}. "
            "O cliente precisa de um vendedor."
        )
        if send_ops_alert(
            db,
            key=f"escalated:{state.id}",
            text=text,
        ):
            sent += 1
    return sent


STUCK_SALE_AFTER = timedelta(minutes=10)
STUCK_SALE_WINDOW = timedelta(hours=24)
_SALE_DONE_STEPS = {"COMPLETED", "ESCALATED"}
_LONG_DIGITS = re.compile(r"\d{6,}")


def stuck_sale_text(*, name: str, phone: str, plan: str, step: str, error: str, conversation_id: int,
                    opportunity_id: int | None) -> str:
    reason = _LONG_DIGITS.sub("<número>", " ".join((error or "").split()))[:240]
    reason = reason or "A venda está aguardando o CRM e não avançou."
    opportunity = f" · oportunidade {opportunity_id}" if opportunity_id else ""
    link = f"\nAbrir conversa: {settings.app_public_url.rstrip('/')}/conversations/{conversation_id}" if settings.app_public_url else ""
    return (
        "⚠️ Venda parada na IA — ERRO NO FECHAMENTO — AÇÃO NECESSÁRIA\n"
        "A IA não conseguiu concluir a venda. Verifique a integração/CRM e assuma este caso.\n"
        f"Cliente: {name or 'não informado'}\n"
        f"Telefone: {phone or 'não informado'}\n"
        f"Plano: {plan or 'não informado'}\n"
        f"Etapa: {step}\n"
        f"Erro: {reason}\n"
        f"Conversa {conversation_id}{opportunity}"
        f"{link}"
    )


def check_stuck_sales(db: Session) -> int:
    """Avisa venda em fechamento que parou por erro ou ficou aguardando o CRM.

    Só conta como parada quando a conversa está sem mensagem nova há 10 minutos,
    para não avisar um erro que a IA resolveu sozinha na tentativa seguinte.
    """
    sent = 0
    now = utcnow()
    states = db.scalars(
        select(AIClosingState).where(
            AIClosingState.completed_at.is_(None),
            AIClosingState.updated_at >= max(now - STUCK_SALE_WINDOW, UNREAD_RECOVERY_SINCE_UTC),
        )
    )
    for state in list(states):
        error = (state.last_error or "").strip()
        if state.step in _SALE_DONE_STEPS or not (error or state.step == "WAITING_CRM_READY"):
            continue
        # O mutirão não deve notificar fechamento sem qualquer interação desde as 18h.
        scoped_message_id = db.scalar(
            select(Message.id).where(
                Message.conversation_id == state.conversation_id,
                Message.created_at >= UNREAD_RECOVERY_SINCE_UTC,
                Message.created_at <= now,
            ).limit(1)
        )
        if scoped_message_id is None:
            continue
        recent = db.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == state.conversation_id,
                Message.created_at >= now - STUCK_SALE_AFTER,
            )
            .limit(1)
        )
        if recent is not None:
            continue
        try:
            collected = json.loads(state.collected_json or "{}")
        except ValueError:
            collected = {}
        conversation = db.get(Conversation, state.conversation_id)
        contact = db.get(Contact, conversation.contact_id) if conversation and conversation.contact_id else None
        text = stuck_sale_text(
            name=str(collected.get("name") or (contact.name if contact else "") or "").strip(),
            phone=_digits(str(collected.get("phone") or (contact.phone if contact else "") or "")),
            plan=str(state.plan_label or ""),
            step=str(state.step or ""),
            error=error,
            conversation_id=state.conversation_id,
            opportunity_id=state.opportunity_id,
        )
        # A chave leva o erro, não a etapa: um erro antigo que ficou gravado não gera
        # aviso novo a cada etapa que a venda avança (venda 27, 15/11).
        reason = hashlib.sha256((error or state.step).encode()).hexdigest()[:12]
        # Mesmo cliente com o mesmo erro: avisa uma vez só (pedido do gestor, 15/11).
        # Erro persistente não pode ficar esquecido para sempre: reavisar no
        # máximo uma vez por dia, ainda que o mesmo erro continue no CRM.
        if send_ops_alert(db, key=f"stuck-sale:{state.id}:{reason}:{now:%Y%m%d}", text=text):
            sent += 1
    return sent


def check_silent_closings(db: Session) -> int:
    """Cliente parou no fechamento e não respondeu aos lembretes: um vendedor liga.

    Avisa uma vez por cliente e por silêncio (a chave muda quando ele volta a responder).
    """
    from app.models import AIFollowupJob  # noqa: PLC0415
    from app.services.ai_followups import (  # noqa: PLC0415
        MAX_UNANSWERED_CLOSING_FOLLOWUPS,
        closing_awaits_customer,
    )

    sent = 0
    now = utcnow()
    states = db.scalars(
        select(AIClosingState).where(
            AIClosingState.completed_at.is_(None),
            AIClosingState.updated_at >= UNREAD_RECOVERY_SINCE_UTC,
        )
    )
    for state in list(states):
        if not closing_awaits_customer(db, state.conversation_id):
            continue
        last_inbound = db.scalar(
            select(Message)
            .where(Message.conversation_id == state.conversation_id, Message.direction == "INBOUND")
            .order_by(Message.id.desc())
            .limit(1)
        )
        reminders = select(AIFollowupJob.sent_at).where(
            AIFollowupJob.conversation_id == state.conversation_id,
            AIFollowupJob.status == "ENVIADO",
            AIFollowupJob.sent_at.is_not(None),
        )
        if not last_inbound or not in_unread_recovery_window(last_inbound.created_at, now=now):
            continue
        reminders = reminders.where(
            AIFollowupJob.sent_at > last_inbound.created_at,
            AIFollowupJob.sent_at >= UNREAD_RECOVERY_SINCE_UTC,
        )
        sent_at = [value for value in db.scalars(reminders) if value]
        if len(sent_at) < MAX_UNANSWERED_CLOSING_FOLLOWUPS:
            continue
        try:
            collected = json.loads(state.collected_json or "{}")
        except ValueError:
            collected = {}
        conversation = db.get(Conversation, state.conversation_id)
        contact = db.get(Contact, conversation.contact_id) if conversation and conversation.contact_id else None
        name = str(collected.get("name") or (contact.name if contact else "") or "").strip()
        phone = _digits(str(collected.get("phone") or (contact.phone if contact else "") or ""))
        text = (
            "📞 Cliente parou no fechamento e não respondeu aos lembretes da IA. Um vendedor precisa ligar.\n"
            f"Cliente: {name or 'não informado'}\n"
            f"Telefone: {phone or 'não informado'}\n"
            f"Plano: {state.plan_label or 'não informado'}\n"
            f"Etapa: {state.step}\n"
            f"Conversa {state.conversation_id}"
            + (f" · oportunidade {state.opportunity_id}" if state.opportunity_id else "")
        )
        key = f"closing-silent:{state.id}:{last_inbound.id if last_inbound is not None else 0}"
        if send_ops_alert(db, key=key, text=text):
            sent += 1
    return sent


UNANSWERED_AFTER = timedelta(minutes=10)
UNANSWERED_WINDOW = timedelta(hours=24)
_CLOSED_STATUSES = {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
_QUIET_AI_STATES = {"OPT_OUT", "COMPLETED"}


def unanswered_customers(db: Session) -> list[dict]:
    """Conversas abertas em que a última mensagem é do cliente há mais de 10 minutos.

    Em 09/11 a fila 1 ficou sem vínculo de IA e dois clientes esperaram sem
    ninguém saber. Inclui filas sem IA e vendedor offline.
    """
    from app.models import ConversationQueueLink, ServiceQueue
    from app.services.ai_reply_delivery import reply_statuses
    from app.services.auto_reply import latest_inbound_is_auto_reply
    from app.services.queue_routing import queue_for_conversation, usable_ai_binding, human_online_in_queue  # noqa: PLC0415

    now = utcnow()
    latest_inbound = db.execute(
        select(Message.conversation_id, Message.id)
        .where(
            Message.direction == "INBOUND",
            Message.created_at >= max(now - UNANSWERED_WINDOW, UNREAD_RECOVERY_SINCE_UTC),
            Message.created_at <= now - UNANSWERED_AFTER,
        )
        .order_by(Message.id.desc())
        .limit(400)
    ).all()
    ai_ids = list(db.scalars(
        select(ConversationQueueLink.conversation_id)
        .join(ServiceQueue, ServiceQueue.id == ConversationQueueLink.queue_id)
        .where(
            ServiceQueue.code == "FILA-IA",
            ConversationQueueLink.conversation_id.in_({cid for cid, _ in latest_inbound}),
        )
    ))
    ai_replies = reply_statuses(db, ai_ids)
    found: list[dict] = []
    seen: set[int] = set()
    for conversation_id, message_id in latest_inbound:
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        ai_reply = ai_replies.get(conversation_id)
        if ai_reply:
            if ai_reply["inbound_id"] != message_id or not ai_reply["attention"]:
                continue
        else:
            later = db.scalar(
                select(Message.id)
                .where(Message.conversation_id == conversation_id, Message.id > message_id)
                .limit(1)
            )
            if later or latest_inbound_is_auto_reply(db, conversation_id):
                continue
        conversation = db.get(Conversation, conversation_id)
        failed = bool(ai_reply and ai_reply["code"] == "failed")
        if not conversation or (
            conversation.status in _CLOSED_STATUSES
            and not (failed and conversation.status == "RESOLVIDO")
        ):
            continue
        ai_state = db.scalar(
            select(AIConversationState.state).where(
                AIConversationState.conversation_id == conversation_id
            )
        )
        if ai_state == "OPT_OUT" or (ai_state == "COMPLETED" and not failed):
            continue
        queue = queue_for_conversation(db, conversation_id)
        binding = usable_ai_binding(db, queue.id) if queue else None
        from app.services.ai_queue_scope import manual_ai_handoff_authorized  # noqa: PLC0415

        manually_delegated = manual_ai_handoff_authorized(db, conversation_id)
        # Filas somente humanas também precisam aparecer no diagnóstico:
        # são justamente as que ficaram sem atendente online.
        inbound_message = db.get(Message, message_id)
        received_at = inbound_message.created_at if inbound_message else None
        if received_at is not None:
            from app.timeutils import utc_naive
            age_minutes = max(0, int((now - utc_naive(received_at)).total_seconds() / 60))
        else:
            age_minutes = 0
        found.append(
            {
                "conversation_id": conversation_id,
                "queue_id": queue.id if queue else None,
                "queue_name": queue.name if queue else "",
                "ai_bound": bool((binding and binding.active) or manually_delegated),
                "age_minutes": age_minutes,
                "assigned_user_id": conversation.assigned_user_id,
                "human_online": human_online_in_queue(db, queue.id) if queue else False,
                "message_type": inbound_message.message_type if inbound_message else "",
                "delivery_failed": failed,
            }
        )
    return found


def check_unanswered_customers(db: Session) -> int:
    """Um aviso por hora enquanto houver cliente esperando resposta."""
    waiting = unanswered_customers(db)
    if not waiting:
        return 0
    for item in waiting:
        LOG.info(
            "ops_unanswered conversation=%s queue=%s ai_bound=%s",
            item["conversation_id"],
            item["queue_id"],
            item["ai_bound"],
        )
    unbound = sorted(
        {f"{item['queue_name'] or 'sem fila'} (id {item['queue_id']})" for item in waiting if not item["ai_bound"]}
    )
    # Casos mais antigos primeiro: o time sabe exatamente onde agir e quem está
    # designado, sem abrir logs técnicos. Não expõe dados pessoais no alerta.
    waiting.sort(key=lambda item: item.get("age_minutes", 0), reverse=True)
    cases = ", ".join(
        f"#{item['conversation_id']} ({item.get('age_minutes', 0)}min"
        f"{'; falha na resposta' if item.get('delivery_failed') else ''}; "
        f"fila {item['queue_id'] or '-'}; "
        f"responsável {item.get('assigned_user_id') or 'sem atribuição'})"
        for item in waiting[:12]
    )
    severe = any(item.get("age_minutes", 0) >= 30 for item in waiting)
    text = (
        f"🔔 MENSAGENS NÃO LIDAS — {len(waiting)} cliente(s) sem resposta "
        "há mais de 10 minutos. "
        f"{'URGENTE: espera de 30min ou mais. ' if severe else ''}"
        f"Prioridades: {cases}."
    )
    offline_count = sum(1 for item in waiting if not item.get("human_online", False))
    if offline_count:
        text += f" Sem vendedor online na fila: {offline_count}."
    if settings.app_public_url:
        text += f" Acessar pendências: {settings.app_public_url.rstrip('/')}/conversations?view=unread."
    if unbound:
        text += " Fila sem IA vinculada: " + "; ".join(unbound) + "."
    # Agregação a cada 30 minutos quando há atraso grave; a cada hora nos
    # demais casos. Deduplicação automática pela chave da auditoria.
    now = utcnow()
    stamp = now.strftime("%Y%m%d%H") + ("0" if now.minute < 30 else "1") if severe else now.strftime("%Y%m%d%H")
    return 1 if send_ops_alert(
        db, key=f"unanswered:{'urgent' if severe else 'normal'}:{stamp}", text=text,
    ) else 0


def check_failed_ai_jobs(db: Session) -> int:
    """Alerta agregado de erros definitivos da IA ainda sem resposta ao cliente.

    Casos corrigidos ou fechados não geram alerta, e IDs/etapas substituem
    dados sensíveis. Evita notificar o cliente com detalhes técnicos.
    """
    since = max(utcnow() - timedelta(hours=24), UNREAD_RECOVERY_SINCE_UTC)
    jobs = list(db.scalars(
        select(AIJob).where(
            AIJob.status == "FALHOU",
            AIJob.updated_at >= since,
        ).order_by(AIJob.id.desc()).limit(120)
    ))
    cases: list[str] = []
    seen: set[int] = set()
    for job in jobs:
        if job.conversation_id in seen:
            continue
        seen.add(job.conversation_id)
        source_message = db.get(Message, job.message_id) if job.message_id else None
        if not source_message or not in_unread_recovery_window(source_message.created_at, now=utcnow()):
            continue
        conv = db.get(Conversation, job.conversation_id)
        if not conv or conv.status in _CLOSED_STATUSES:
            continue
        state = db.scalar(
            select(AIConversationState.state).where(
                AIConversationState.conversation_id == job.conversation_id
            )
        )
        if state in _QUIET_AI_STATES:
            continue
        response = db.scalar(
            select(Message.id).where(
                Message.conversation_id == job.conversation_id,
                Message.direction == "OUTBOUND",
                Message.id > job.message_id,
            ).limit(1)
        )
        if response:
            continue
        reason = _LONG_DIGITS.sub("<número>", " ".join((job.error or "falha no processamento").split()))[:110]
        cases.append(f"#{job.conversation_id} (fila {job.queue_id}; {reason})")
        if len(cases) >= 10:
            break
    if not cases:
        return 0
    digest = hashlib.sha256("|".join(cases).encode("utf-8")).hexdigest()[:12]
    text = (
        "⚠️ ERRO DE ATENDIMENTO IA — AÇÃO DO GESTOR\n"
        "A IA não conseguiu responder às conversas abaixo; revisar o motivo e continuar o atendimento.\n"
        + "\n".join(cases)
    )
    if settings.app_public_url:
        text += f"\nCaixa de entrada: {settings.app_public_url.rstrip('/')}/conversations?view=unread"
    key = f"ai-job-errors:{utcnow():%Y%m%d}:{digest}"
    return 1 if send_ops_alert(db, key=key, text=text) else 0


def _in_period(value, start, end) -> bool:
    """Compara datas mesmo quando o banco devolve o horário sem fuso (SQLite)."""
    from datetime import timezone  # noqa: PLC0415

    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return start <= value < end


def ai_daily_funnel(db: Session, *, start, end) -> dict:
    """Funil da IA em um período: do primeiro contato à venda assinada."""
    from app.models import AIAgentProfile, AIConversationState  # noqa: PLC0415

    ai_user_ids = [
        user_id
        for user_id in db.scalars(select(AIAgentProfile.user_id))
        if user_id
    ]
    attended = set(
        db.scalars(
            select(Message.conversation_id)
            .join(AIConversationState, AIConversationState.conversation_id == Message.conversation_id)
            .where(
                Message.direction == "INBOUND",
                Message.created_at >= start,
                Message.created_at < end,
            )
            .distinct()
        )
    )
    replies = 0
    if ai_user_ids:
        replies = len(
            list(
                db.scalars(
                    select(Message.id).where(
                        Message.direction == "OUTBOUND",
                        Message.sender_user_id.in_(ai_user_ids),
                        Message.created_at >= start,
                        Message.created_at < end,
                    )
                )
            )
        )
    closings = list(
        db.scalars(
            select(AIClosingState).where(
                AIClosingState.updated_at >= start, AIClosingState.updated_at < end
            )
        )
    )
    started = sum(1 for item in closings if _in_period(item.created_at, start, end))
    credit_rejected = sum(1 for item in closings if item.step == "CREDIT_REJECTED")
    contract_sent = sum(1 for item in closings if str(item.sign_url or "").strip())
    won = 0
    for item in closings:
        if item.step != "COMPLETED" or not item.completed_at:
            continue
        try:
            collected = json.loads(item.collected_json or "{}")
        except ValueError:
            collected = {}
        if collected.get("_won_after_signature") and _in_period(item.completed_at, start, end):
            won += 1
    opt_out = len(
        list(
            db.scalars(
                select(AIConversationState.id).where(
                    AIConversationState.state == "OPT_OUT",
                    AIConversationState.updated_at >= start,
                    AIConversationState.updated_at < end,
                )
            )
        )
    )
    return {
        "attended": len(attended),
        "replies": replies,
        "closing_started": started,
        "credit_rejected": credit_rejected,
        "contract_sent": contract_sent,
        "won": won,
        "opt_out": opt_out,
    }


def daily_funnel_text(funnel: dict, day_label: str) -> str:
    attended = funnel["attended"]
    rate = f"{funnel['won'] / attended * 100:.1f}%" if attended else "sem base"
    return (
        f"📊 Funil da IA em {day_label}\n"
        f"Clientes atendidos: {attended}\n"
        f"Respostas enviadas: {funnel['replies']}\n"
        f"Entraram em contratação: {funnel['closing_started']}\n"
        f"Crédito reprovado: {funnel['credit_rejected']}\n"
        f"Contratos enviados: {funnel['contract_sent']}\n"
        f"Vendas assinadas: {funnel['won']}\n"
        f"Pediram para parar: {funnel['opt_out']}\n"
        f"Conversão (vendas / atendidos): {rate}"
    )


def check_daily_funnel(db: Session) -> int:
    """Uma vez por dia, depois das 8h (horário de Brasília), manda o funil de ontem."""
    from datetime import datetime, timezone  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    zone = ZoneInfo(settings.default_timezone or "America/Sao_Paulo")
    local_now = datetime.now(zone)
    if local_now.hour < 8:
        return 0
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (today_start - timedelta(days=1)).astimezone(timezone.utc)
    end = today_start.astimezone(timezone.utc)
    key = f"funnel:{(today_start - timedelta(days=1)).strftime('%Y%m%d')}"
    if _already_sent(db, key, None):
        return 0
    funnel = ai_daily_funnel(db, start=start, end=end)
    label = (today_start - timedelta(days=1)).strftime("%d/%m")
    return 1 if send_ops_alert(db, key=key, text=daily_funnel_text(funnel, label)) else 0


def run_ops_alert_checks(db: Session) -> int:
    sent = check_instances(db) + check_sales_and_escalations(db)
    for check in (check_stuck_sales, check_failed_ai_jobs, check_silent_closings, check_unanswered_customers, check_daily_funnel):
        try:
            sent += check(db)
        except Exception:  # noqa: BLE001 - um aviso com erro não pode parar os outros
            LOG.exception("ops_alert_check_failed check=%s", check.__name__)
            db.rollback()
    return sent
