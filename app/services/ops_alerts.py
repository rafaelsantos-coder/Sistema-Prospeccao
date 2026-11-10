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


def run_ops_alert_checks(db: Session) -> int:
    return check_instances(db) + check_sales_and_escalations(db)
