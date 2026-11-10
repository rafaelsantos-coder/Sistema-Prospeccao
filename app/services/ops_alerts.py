"""Avisos operacionais por WhatsApp para o responsável do sistema.

Eventos cobertos:
- instância Z-API desconectou ou reconectou;
- venda finalizada pela IA (assinada e confirmada como ganha);
- conversa escalada para atendimento humano.

O aviso sai por qualquer instância Z-API ativa e conectada (nunca pela que
caiu). Cada aviso tem chave de deduplicação gravada na auditoria, então o
mesmo evento não é enviado duas vezes. Não leva nome, telefone nem CPF do
cliente: só identificadores técnicos, plano e valor.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIClosingState, AuditLog, Message, ZApiInstance
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
) -> bool:
    """Envia o aviso uma vez por chave. Devolve True quando enviou."""
    phone = _digits(settings.ops_alert_phone)
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
            text = f"✅ Sulnet Prospecta: o WhatsApp \"{instance.name}\" voltou a ficar conectado."
        else:
            text = (
                f"⚠️ Sulnet Prospecta: o WhatsApp \"{instance.name}\" DESCONECTOU. "
                "Atendimentos e disparos por esse número estão parados até reconectar no painel da Z-API."
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
    since = utcnow() - timedelta(hours=24)
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


UNANSWERED_AFTER = timedelta(minutes=10)
UNANSWERED_WINDOW = timedelta(hours=6)
_CLOSED_STATUSES = {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
_QUIET_AI_STATES = {"OPT_OUT", "COMPLETED"}


def unanswered_customers(db: Session) -> list[dict]:
    """Conversas abertas em que a última mensagem é do cliente há mais de 10 minutos.

    Em 09/11 a fila 1 ficou sem vínculo de IA e dois clientes esperaram sem
    ninguém saber. Fila em modo somente humano fica de fora.
    """
    from app.models import AIConversationState, AIQueueBinding, Conversation  # noqa: PLC0415
    from app.services.queue_routing import queue_for_conversation  # noqa: PLC0415

    now = utcnow()
    latest_inbound = db.execute(
        select(Message.conversation_id, Message.id)
        .where(
            Message.direction == "INBOUND",
            Message.message_type == "TEXT",
            Message.created_at >= now - UNANSWERED_WINDOW,
            Message.created_at <= now - UNANSWERED_AFTER,
        )
        .order_by(Message.id.desc())
        .limit(400)
    ).all()
    found: list[dict] = []
    seen: set[int] = set()
    for conversation_id, message_id in latest_inbound:
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        later = db.scalar(
            select(Message.id)
            .where(Message.conversation_id == conversation_id, Message.id > message_id)
            .limit(1)
        )
        if later:
            continue
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.status in _CLOSED_STATUSES:
            continue
        from app.services.auto_reply import latest_inbound_is_auto_reply  # noqa: PLC0415

        if latest_inbound_is_auto_reply(db, conversation_id):
            continue
        ai_state = db.scalar(
            select(AIConversationState.state).where(
                AIConversationState.conversation_id == conversation_id
            )
        )
        if ai_state in _QUIET_AI_STATES:
            continue
        queue = queue_for_conversation(db, conversation_id)
        binding = (
            db.scalar(select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id))
            if queue
            else None
        )
        if binding and binding.active and binding.handling_mode == "HUMAN":
            continue
        found.append(
            {
                "conversation_id": conversation_id,
                "queue_id": queue.id if queue else None,
                "queue_name": queue.name if queue else "",
                "ai_bound": bool(binding and binding.active),
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
    ids = ", ".join(str(item["conversation_id"]) for item in waiting[:12])
    text = f"⚠️ {len(waiting)} cliente(s) sem resposta há mais de 10 minutos. Conversas: {ids}."
    if unbound:
        text += " Fila sem IA vinculada: " + "; ".join(unbound) + "."
    stamp = utcnow().strftime("%Y%m%d%H")
    return 1 if send_ops_alert(db, key=f"unanswered:{stamp}", text=text) else 0


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
    for check in (check_unanswered_customers, check_daily_funnel):
        try:
            sent += check(db)
        except Exception:  # noqa: BLE001 - um aviso com erro não pode parar os outros
            LOG.exception("ops_alert_check_failed check=%s", check.__name__)
            db.rollback()
    return sent
