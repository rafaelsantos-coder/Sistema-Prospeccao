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
    if "aprovad" in str(getattr(state, "credit_status", "") or "").casefold():
        notes.append(("credit_approved", "Crédito aprovado", "Análise de crédito aprovada."))
    if step in {"WAITING_CONTRACT", "WAITING_SIGNATURE"}:
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


_OBJECTIONS = {
    "PRICE": "preço",
    "NO_INTEREST": "sem interesse",
    "CURRENT_PROVIDER": "já tem provedor",
    "FIDELITY": "fidelidade",
    "TIMING": "momento/adiou",
    "DECISION_MAKER": "outro decisor",
    "TRUST": "confiança",
    "INSTALLATION": "instalação",
    "MONEY_NOW": "sem dinheiro agora",
    "OTHER": "outra",
}


def sdr_note_text(decision) -> tuple[str, str] | None:
    """Resumo comercial da última decisão do SDR para o CRM, sem documento nem telefone."""
    q = decision.qualification
    parts: list[str] = []
    objection = _OBJECTIONS.get(str(decision.objection or "NONE"))
    if objection:
        parts.append(f"Objeção: {objection}.")
    current = " / ".join(
        item for item in (q.current_provider, q.current_speed, q.current_price) if item
    )
    if current:
        parts.append(f"Situação atual do cliente: {current}.")
    if decision.offer_plan_code:
        free = f", {decision.free_months} mês(es) grátis" if decision.free_months else ""
        parts.append(f"Plano ofertado: {decision.offer_plan_code}{free}.")
    where = " / ".join(item for item in (q.city, q.customer_type) if item)
    if where:
        parts.append(f"Cidade/segmento: {where}.")
    if decision.proposal_summary:
        parts.append(f"Proposta: {decision.proposal_summary[:300]}")
    if not parts:
        return None
    if decision.next_action:
        parts.append(f"Próximo passo: {decision.next_action[:200]}")
    result = "Objeção e oferta" if objection else "Atualização comercial"
    return result, " ".join(parts)


def record_sdr_note(db: Session, conversation_id: int, decision) -> None:
    """Uma nota por mudança relevante da conversa; nunca derruba o atendimento."""
    import hashlib  # noqa: PLC0415

    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.models import AuditLog  # noqa: PLC0415
    from app.services import crm_prospect_sync  # noqa: PLC0415
    from app.services.audit import write_audit  # noqa: PLC0415

    try:
        built = sdr_note_text(decision)
        if not built:
            return
        opportunity_id = crm_prospect_sync.synced_opportunity_id(db, conversation_id)
        if not opportunity_id:
            return
        result, text = built
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        seen = db.scalar(
            _select(AuditLog.id)
            .where(
                AuditLog.action == "CRM_NOTE_SDR",
                AuditLog.entity_id == str(conversation_id),
                AuditLog.payload_json.contains(digest),
            )
            .limit(1)
        )
        if seen:
            return
        SulnetCRMClient.from_settings().add_opportunity_note(
            opportunity_id, result=result, description=text
        )
        write_audit(
            db,
            user=None,
            action="CRM_NOTE_SDR",
            entity_type="conversation",
            entity_id=conversation_id,
            payload={"digest": digest},
        )
    except CRMIntegrationError as exc:
        LOG.warning("crm_note_sdr_failed conversation=%s status=%s", conversation_id, exc.status_code)
    except Exception:  # noqa: BLE001
        LOG.exception("crm_note_sdr_error conversation=%s", conversation_id)
