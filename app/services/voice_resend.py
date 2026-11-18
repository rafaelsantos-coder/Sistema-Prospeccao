"""Selecao conservadora de contatos para uma nova rodada de campanha de voz.

Nao altera VoiceRecipient original. Reenvio de nao atendidos exige evidencias
persistentes de tentativa, resultado sem atendimento e ausencia de qualquer
evidencia historica de atendimento, inclusive erros posteriores ao atendimento.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    GlobalContactBlacklist, Lead, VoiceCallEvent, VoiceCampaign, VoiceRecipient,
)
from app.services.voice_test import validate_test_phone
from app.services.global_blacklist import canonical_phone


# SIP 200 / midia ativa / DTMF / REFER demonstram atendimento, mesmo que
# o resultado final tenha sido falha de transferencia, audio ou timeout.
ANSWER_EVIDENCE_EVENTS = frozenset({
    "ATENDIDA", "CONFIRMADO_DTMF", "REPRODUZINDO_AUDIO",
    "TRANSFERINDO", "TRANSFERIDA", "FINALIZADA",
    "AUDIO_CONCLUIDO", "AUDIO_INTERROMPIDO", "FALHA_AUDIO",
    "FALHA_TRANSFERENCIA", "SEM_INTERESSE",
})
# Estes finais nao demonstram atendimento. TIMEOUT e elegivel apenas se
# nao houver evento/coluna de atendimento em qualquer campanha de voz.
NO_ANSWER_STATES = frozenset({
    "SEM_ATENDIMENTO", "SEM_RESPOSTA", "OCUPADO", "TIMEOUT",
})
ACTIVE_STATES = frozenset({
    "INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO",
})
RESEND_MODES = frozenset({"ALL", "UNANSWERED"})


@dataclass
class ResendSelection:
    mode: str
    total: int
    eligible: list[VoiceRecipient]
    blocked: int = 0
    answered_excluded: int = 0
    never_attempted: int = 0
    other_excluded: int = 0

    @property
    def eligible_count(self) -> int:
        return len(self.eligible)


def select_resend_recipients(db: Session, campaign: VoiceCampaign, mode: str) -> ResendSelection:
    """Obtém a amostra sem alterar dados; chamada dentro de transação no POST."""
    if mode not in RESEND_MODES:
        raise ValueError("Modalidade de reenvio inválida.")
    rows = list(db.scalars(
        select(VoiceRecipient).where(
            VoiceRecipient.campaign_id == campaign.id,
        ).order_by(VoiceRecipient.id.asc())
    ))
    selection = ResendSelection(mode=mode, total=len(rows), eligible=[])
    if not rows:
        return selection
    phones = sorted({row.phone for row in rows if row.phone})
    # Compatibilidade com bases legadas em DDD+número e E.164.
    lookup_phones = set(phones)
    for phone in phones:
        try:
            canonical = canonical_phone(phone)
            lookup_phones.add(canonical)
            if canonical.startswith("55"):
                lookup_phones.add(canonical[2:])
        except ValueError:
            continue

    # A lista central e o opt-out legado sao consultados em lote. O worker
    # checa novamente antes do INVITE (a blacklist pode mudar posteriormente).
    blocked = set(db.scalars(select(GlobalContactBlacklist.phone).where(
        GlobalContactBlacklist.active.is_(True),
        GlobalContactBlacklist.phone.in_(lookup_phones),
    )))
    blocked.update(db.scalars(select(Lead.phone).where(
        Lead.do_not_contact.is_(True),
        Lead.phone.in_(lookup_phones),
    )))

    answered_phones: set[str] = set()
    ringing_recipient_ids: set[int] = set()
    if mode == "UNANSWERED":
        # Evita novos contatos com numero que ja atendeu em qualquer rodada,
        # mesmo se falhou a transferencia ou se o status foi sobrescrito.
        answered_phones.update(db.scalars(select(VoiceRecipient.phone).where(
            VoiceRecipient.phone.in_(lookup_phones),
            (
                VoiceRecipient.answered_at.is_not(None)
                | VoiceRecipient.transferred_at.is_not(None)
                | VoiceRecipient.status.in_(ANSWER_EVIDENCE_EVENTS)
                | VoiceRecipient.last_event.in_(ANSWER_EVIDENCE_EVENTS)
            ),
        )))
        answered_phones.update(db.scalars(select(VoiceRecipient.phone).join(
            VoiceCallEvent, VoiceCallEvent.recipient_id == VoiceRecipient.id,
        ).where(
            VoiceRecipient.phone.in_(lookup_phones),
            VoiceCallEvent.event.in_(ANSWER_EVIDENCE_EVENTS),
        )))
        ringing_recipient_ids.update(db.scalars(select(VoiceCallEvent.recipient_id).where(
            VoiceCallEvent.recipient_id.in_([row.id for row in rows]),
            VoiceCallEvent.event == "TOCANDO",
        )))

    # Telefones respondidos sao normalizados antes da comparacao.
    normalized_answered: set[str] = set()
    for phone in answered_phones:
        try:
            normalized_answered.add(canonical_phone(phone))
        except ValueError:
            continue

    for row in rows:
        try:
            validate_test_phone(row.phone)
            normalized = canonical_phone(row.phone)
        except ValueError:
            selection.blocked += 1
            continue
        if normalized in blocked or row.status == "BLOQUEADA":
            selection.blocked += 1
            continue
        if row.status in ACTIVE_STATES:
            selection.other_excluded += 1
            continue

        if mode == "ALL":
            selection.eligible.append(row)
            continue

        if normalized in normalized_answered:
            selection.answered_excluded += 1
        elif not row.attempts:
            selection.never_attempted += 1
        elif row.status in NO_ANSWER_STATES or (
            row.status == "FALHOU" and row.id in ringing_recipient_ids
        ):
            selection.eligible.append(row)
        else:
            # Indeterminado, falha de registro SIP, audio interrompido sem
            # evidencias suficientes ou outro evento inconclusivo: nao discar.
            selection.other_excluded += 1

    return selection
