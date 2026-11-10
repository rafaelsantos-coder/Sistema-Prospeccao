"""Anotações da IA na linha do tempo da oportunidade do CRM.

Só nos marcos da venda (plano, crédito, contrato, humano), uma vez cada, para o
vendedor que assumir ler tudo sem abrir o WhatsApp. Nunca trava a conversa.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import AIClosingState
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient

LOG = logging.getLogger("sulnet_crm_notes")
NOTES_KEY = "_crm_notes"


def _money(value: Any) -> str:
    try:
        return f"R$ {float(value):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def pending_notes(state: AIClosingState, collected: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Marcos atuais da venda: (chave, resultado, descrição), sem dados pessoais."""
    notes: list[tuple[str, str, str]] = []
    if state.plan_label:
        price = _money(state.offer_price)
        text = f"Cliente escolheu {state.plan_label}" + (f" por {price}/mês." if price else ".")
        notes.append((f"plan:{state.plan_label}", "Plano escolhido", text))
    step = state.step
    if step == "CREDIT_REVIEW":
        notes.append(("credit_review", "Consulta de crédito", "Consulta de crédito em andamento."))
    elif step == "CREDIT_REJECTED":
        alt = collected.get("_credit_alt")
        extra = {
            "FEE": " Cliente escolheu pagar a taxa de R$ 150,00; atendimento humano deve ligar.",
            "PROOF_PENDING": " Cliente vai enviar comprovante do provedor.",
            "PROOF_SENT": " Cliente enviou o comprovante; atendimento humano deve conferir.",
        }.get(str(alt), " Cliente decide entre comprovante, taxa de R$ 150,00 ou outro CPF.")
        notes.append((f"credit_rejected:{alt or '-'}", "Crédito reprovado", "Crédito reprovado." + extra))
    elif step in {"WAITING_CONTRACT", "WAITING_SIGNATURE"}:
        notes.append(("contract_sent", "Contrato enviado", "Contrato gerado e enviado para assinatura."))
    elif step == "ESCALATED":
        notes.append(("human", "Pedido de atendimento humano", "Cliente pediu atendimento humano."))
    if step == "COMPLETED" and collected.get("_won_after_signature"):
        notes.append(("signed", "Contrato assinado", "Contrato assinado pelo cliente; venda concluída."))
    return notes


def record_milestones(db: Session, state: AIClosingState) -> int:
    """Escreve no CRM os marcos ainda não anotados. Devolve quantas notas gravou."""
    if not state.opportunity_id:
        return 0
    try:
        collected = json.loads(state.collected_json or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(collected, dict):
        return 0
    done = set(collected.get(NOTES_KEY) or [])
    todo = [note for note in pending_notes(state, collected) if note[0] not in done]
    if not todo:
        return 0
    client = SulnetCRMClient.from_settings()
    written = 0
    for key, result, description in todo:
        try:
            client.add_opportunity_note(state.opportunity_id, result=result, description=description)
        except CRMIntegrationError as exc:
            LOG.warning("crm_note_failed opportunity=%s status=%s", state.opportunity_id, exc.status_code)
            # Rota ausente ou CRM fora: tenta de novo no próximo ciclo, sem insistir agora.
            break
        done.add(key)
        written += 1
    if written:
        collected[NOTES_KEY] = sorted(done)
        state.collected_json = json.dumps(collected, ensure_ascii=False, default=str)
        db.flush()
    return written


def record_milestones_safe(db: Session, conversation_id: int) -> None:
    from sqlalchemy import select  # noqa: PLC0415

    try:
        state = db.scalar(select(AIClosingState).where(AIClosingState.conversation_id == conversation_id))
        if state:
            record_milestones(db, state)
    except Exception:  # noqa: BLE001 - anotação nunca derruba o atendimento
        LOG.exception("crm_notes_error conversation=%s", conversation_id)
