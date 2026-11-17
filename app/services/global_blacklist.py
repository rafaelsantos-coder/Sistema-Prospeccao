"""Blacklist global por telefone, independente de fila, canal comercial ou Z-API.

Todos os envios do sistema consultam a lista central imediatamente antes de
chamar o provedor. Em caso de falha de banco, o envio é interrompido (fail-closed).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    AIFollowupJob,
    Contact,
    Conversation,
    GlobalContactBlacklist,
    Lead,
    TelegramLink,
)
from app.timeutils import utcnow


class BlacklistPhoneError(ValueError):
    pass


class BlacklistedRecipientError(RuntimeError):
    """Bloqueio definitivo no envio (não deve ser retentado)."""


def canonical_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) in {10, 11}:
        digits = "55" + digits
    if len(digits) not in range(12, 16):
        raise BlacklistPhoneError("Telefone inválido: informe DDD e número, com ou sem DDI.")
    if digits.startswith("55") and len(digits) not in {12, 13}:
        raise BlacklistPhoneError("Número brasileiro inválido.")
    return digits


def is_globally_blocked(db: Session, phone: str | None) -> bool:
    """O mesmo contato nunca sai por outra fila ou instância."""
    number = canonical_phone(phone)
    row = db.scalar(
        select(GlobalContactBlacklist.active).where(GlobalContactBlacklist.phone == number)
    )
    if row is not None:
        return bool(row)
    # Proteção para opt-outs legados feitos antes da blacklist central.
    return bool(db.scalar(
        select(Lead.id).where(
            Lead.phone == number,
            Lead.do_not_contact.is_(True),
        ).limit(1)
    ))


def assert_outbound_permitted(phone: str | None) -> None:
    """Guarda de última milha: utilizada pelo remetente compartilhado por todas as filas."""
    try:
        with SessionLocal() as db:
            blocked = is_globally_blocked(db, phone)
    except BlacklistPhoneError as exc:
        raise BlacklistedRecipientError(str(exc)) from exc
    except Exception as exc:
        raise BlacklistedRecipientError(
            "Envio suspenso: não foi possível validar a blacklist global."
        ) from exc
    if blocked:
        raise BlacklistedRecipientError(
            "Contato incluído na blacklist global. Nenhuma mensagem foi enviada."
        )


def is_telegram_chat_blocked(db: Session, chat_id: str) -> bool:
    """O bot Telegram também respeita a blacklist para chats vinculados a um lead."""
    phone = db.scalar(
        select(Lead.phone)
        .join(TelegramLink, TelegramLink.lead_id == Lead.id)
        .where(TelegramLink.chat_id == str(chat_id)).limit(1)
    )
    return is_globally_blocked(db, phone) if phone else False


def assert_telegram_outbound_permitted(chat_id: str) -> None:
    try:
        with SessionLocal() as db:
            blocked = is_telegram_chat_blocked(db, chat_id)
    except Exception as exc:
        raise BlacklistedRecipientError(
            "Envio suspenso: não foi possível validar a blacklist global."
        ) from exc
    if blocked:
        raise BlacklistedRecipientError(
            "Contato incluído na blacklist global. Nenhuma mensagem foi enviada."
        )


def block_phone(
    db: Session, *, phone: str, name: str = "", reason: str = "",
    source: str = "MANUAL", actor_id: int | None = None,
) -> tuple[GlobalContactBlacklist, bool]:
    """Upsert idempotente e reativação segura. Todas as filas compartilham a linha."""
    number = canonical_phone(phone)
    row = db.scalar(select(GlobalContactBlacklist).where(
        GlobalContactBlacklist.phone == number
    ))
    created = not bool(row and row.active)
    now = utcnow()
    if row is None:
        row = GlobalContactBlacklist(phone=number, created_at=now)
        db.add(row)
    if created:
        row.active = True
        row.name = (name or row.name or "").strip()[:150]
        row.reason = (reason or "Contato não deseja receber mensagens").strip()[:300]
        row.source = (source or "MANUAL").strip().upper()[:24]
        row.created_by_id = actor_id
        row.created_at = now
        row.removed_at = None
        row.removed_by_id = None
        row.updated_at = now
        db.flush()
        # Persistência no CRM existente e cancelamento dos follow-ups programados.
        db.execute(
            update(Lead).where(Lead.phone == number).values(
                do_not_contact=True,
                stage="NAO_CONTATAR",
                next_action_at=None,
            )
        )
        ids = list(db.scalars(
            select(Conversation.id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Contact.phone == number)
        ))
        if ids:
            db.execute(
                update(AIFollowupJob)
                .where(
                    AIFollowupJob.conversation_id.in_(ids),
                    AIFollowupJob.status.in_(("PENDENTE", "PROCESSANDO")),
                )
                .values(status="CANCELADO", error="BLACKLIST_GLOBAL")
            )
    return row, created


def remove_phone_block(db: Session, *, entry: GlobalContactBlacklist, actor_id: int) -> None:
    """Remoção apenas gerencial com auditoria; restaura estado do bloqueio central."""
    if not entry.active:
        return
    entry.active = False
    entry.removed_by_id = actor_id
    entry.removed_at = utcnow()
    entry.updated_at = utcnow()
    # Desbloqueio explícito também libera apenas o opt-out legado do mesmo número.
    # Nenhuma fila, disparo ou follow-up é retomado automaticamente.
    db.execute(
        update(Lead).where(Lead.phone == entry.phone).values(do_not_contact=False)
    )


_PHONE_HEADERS = {"telefone", "numero", "número", "celular", "whatsapp", "phone", "fone", "contato"}
_NAME_HEADERS = {"nome", "name", "cliente", "contato_nome", "empresa", "razao_social"}
_REASON_HEADERS = {"motivo", "reason", "observacao", "observação", "justificativa"}


def parse_blacklist_csv(payload: bytes, *, max_rows: int = 10000) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Aceita CSV com cabeçalho telefone/numero/celular/whatsapp ou coluna única."""
    if not payload or len(payload) > 2 * 1024 * 1024:
        raise ValueError("CSV vazio ou maior que 2 MB.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("O CSV deve utilizar codificação UTF-8.") from exc
    if "\x00" in text:
        raise ValueError("CSV inválido.")
    if not text.strip():
        raise ValueError("CSV sem registros.")
    sample = text[:2048]
    try:
        separator = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        separator = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=separator))
    if not rows:
        raise ValueError("CSV sem registros.")
    headers = [re.sub(r"\s+", "_", col.strip().casefold()) for col in rows[0]]
    phone_idx = next((i for i, col in enumerate(headers) if col in _PHONE_HEADERS), None)
    name_idx = next((i for i, col in enumerate(headers) if col in _NAME_HEADERS), None)
    reason_idx = next((i for i, col in enumerate(headers) if col in _REASON_HEADERS), None)
    has_header = phone_idx is not None
    if not has_header:
        # Uma coluna por linha, sem cabeçalho; nunca adivinhar múltiplas colunas.
        if len(rows[0]) != 1:
            raise ValueError("Inclua uma coluna 'telefone', 'celular', 'numero' ou 'whatsapp'.")
        phone_idx = 0
    seen: set[str] = set()
    parsed: list[dict[str, str]] = []
    summary = {"linhas": 0, "invalidos": 0, "duplicados_arquivo": 0}
    for values in rows[1:] if has_header else rows:
        if not values or not any(cell.strip() for cell in values):
            continue
        summary["linhas"] += 1
        if summary["linhas"] > max_rows:
            raise ValueError(f"O limite por importação é de {max_rows} contatos.")
        raw = values[phone_idx].strip() if phone_idx < len(values) else ""
        try:
            number = canonical_phone(raw)
        except BlacklistPhoneError:
            summary["invalidos"] += 1
            continue
        if number in seen:
            summary["duplicados_arquivo"] += 1
            continue
        seen.add(number)
        parsed.append({
            "phone": number,
            "name": values[name_idx].strip()[:150] if name_idx is not None and name_idx < len(values) else "",
            "reason": values[reason_idx].strip()[:300] if reason_idx is not None and reason_idx < len(values) else "",
        })
    return parsed, summary
