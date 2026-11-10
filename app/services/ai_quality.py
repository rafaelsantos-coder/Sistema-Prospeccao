"""Nota automática por conversa atendida pela IA.

Mede o que o diagnóstico de 03/11 apontou: repetição, bloqueios do Guardião,
falhas, frustração do cliente, follow-ups sem resposta e até onde a venda foi.
É calculada na hora a partir do banco, sem tabela nova, e alimenta o painel
"Qualidade das conversas" e a escolha dos casos de teste (as piores conversas).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIClosingState,
    AIConversationState,
    AIFollowupJob,
    AIRun,
    AuditLog,
    Conversation,
    Message,
)
from app.timeutils import utc_naive, utcnow

FRUSTRATION_CUES = (
    "perguntas demais",
    "ja falei",
    "ja respondi",
    "ja disse",
    "nao entendeu",
    "voce nao entende",
    "de novo isso",
    "mesma pergunta",
    "para de mandar",
    "pare de mandar",
    "chato",
    "nao vou querer mais",
    "desisto",
    "vai desistir",
    "eu nem contratei",
    "robo",
)

OUTCOME_LABELS = {
    "GANHO": "Venda ganha",
    "CONTRATO_ENVIADO": "Contrato enviado",
    "FECHAMENTO": "No fechamento",
    "HUMANO": "Com humano",
    "ATIVA": "Em andamento",
    "SILENCIO": "Cliente em silêncio",
    "ENCERRADA": "Encerrada",
}


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass
class ConversationQuality:
    conversation_id: int
    name: str
    score: int
    outcome: str
    inbound: int = 0
    outbound: int = 0
    repeated_texts: int = 0
    guardian_blocks: int = 0
    failed_runs: int = 0
    runs: int = 0
    frustration: list[str] = field(default_factory=list)
    unanswered_streak: int = 0
    followups_sent: int = 0
    silent_hours: float | None = None
    stuck: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, self.outcome)


def _outcome(
    conversation: Conversation,
    closing: AIClosingState | None,
    ai_state: AIConversationState | None,
    silent_hours: float | None,
    last_is_outbound: bool,
) -> str:
    lead_stage = conversation.lead.stage if conversation.lead else ""
    if lead_stage == "GANHO" or (closing and closing.step == "COMPLETED"):
        return "GANHO"
    if closing and closing.step == "WAITING_SIGNATURE":
        return "CONTRATO_ENVIADO"
    if closing and closing.step not in {"ESCALATED", "COMPLETED"}:
        return "FECHAMENTO"
    if (ai_state and ai_state.state in {"HANDOFF", "HUMAN_ACTIVE", "HANDOFF_HUMAN"}) or (
        closing and closing.step == "ESCALATED"
    ):
        return "HUMANO"
    if conversation.status in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}:
        return "ENCERRADA"
    if last_is_outbound and silent_hours is not None and silent_hours >= 24:
        return "SILENCIO"
    return "ATIVA"


def score_conversation(
    conversation: Conversation,
    messages: list[Message],
    runs: list[AIRun],
    *,
    closing: AIClosingState | None,
    ai_state: AIConversationState | None,
    followups_sent: int,
) -> ConversationQuality:
    now = utcnow()
    inbound = [item for item in messages if item.direction == "INBOUND"]
    outbound = [item for item in messages if item.direction == "OUTBOUND"]

    repeated = Counter(
        _fold(item.body) for item in outbound if len(_fold(item.body)) >= 12
    )
    repeated_texts = sum(count - 1 for count in repeated.values() if count > 1)

    frustration: list[str] = []
    for item in inbound:
        folded = _fold(item.body)
        frustration.extend(cue for cue in FRUSTRATION_CUES if cue in folded)

    streak = 0
    for item in reversed(messages):
        if item.direction == "INBOUND":
            break
        streak += 1

    guardian_blocks = sum(
        1 for run in runs if run.status == "FALHOU" and "guardi" in _fold(run.error)
    )
    failed_runs = sum(1 for run in runs if run.status == "FALHOU")

    last_inbound_at = utc_naive(inbound[-1].created_at) if inbound else None
    silent_hours = (
        round((now - last_inbound_at).total_seconds() / 3600, 1) if last_inbound_at else None
    )
    last_is_outbound = bool(messages) and messages[-1].direction == "OUTBOUND"
    outcome = _outcome(conversation, closing, ai_state, silent_hours, last_is_outbound)

    score = 100
    score -= min(30, 10 * repeated_texts)
    score -= min(20, 10 * guardian_blocks)
    score -= min(15, 5 * max(0, failed_runs - guardian_blocks))
    score -= 20 if frustration else 0
    score -= 10 if streak > 3 else 0
    score += {"GANHO": 10, "CONTRATO_ENVIADO": 5}.get(outcome, 0)
    score = max(0, min(100, score))

    stuck = bool(
        outcome in {"ATIVA", "FECHAMENTO", "SILENCIO"}
        and last_is_outbound
        and silent_hours is not None
        and silent_hours >= 24
    )

    flags: list[str] = []
    if repeated_texts:
        flags.append(f"{repeated_texts} texto(s) repetido(s)")
    if guardian_blocks:
        flags.append(f"{guardian_blocks} bloqueio(s) do Guardião")
    if failed_runs - guardian_blocks > 0:
        flags.append(f"{failed_runs - guardian_blocks} execução(ões) com falha")
    if frustration:
        flags.append("Cliente demonstrou frustração")
    if streak > 3:
        flags.append(f"{streak} mensagens seguidas sem resposta")
    if stuck:
        flags.append("Parada há mais de 24 h")

    contact = conversation.contact
    name = (
        (contact.business_name or contact.name or contact.phone) if contact else ""
    ) or f"Conversa {conversation.id}"
    return ConversationQuality(
        conversation_id=conversation.id,
        name=name,
        score=score,
        outcome=outcome,
        inbound=len(inbound),
        outbound=len(outbound),
        repeated_texts=repeated_texts,
        guardian_blocks=guardian_blocks,
        failed_runs=failed_runs,
        runs=len(runs),
        frustration=sorted(set(frustration)),
        unanswered_streak=streak,
        followups_sent=followups_sent,
        silent_hours=silent_hours,
        stuck=stuck,
        flags=flags,
    )


def conversation_quality_report(db: Session, *, days: int = 7) -> dict:
    since = utcnow() - timedelta(days=max(1, days))
    conversation_ids = list(
        dict.fromkeys(
            db.scalars(
                select(AIRun.conversation_id)
                .where(AIRun.created_at >= since)
                .order_by(AIRun.created_at.desc())
            )
        )
    )
    rows: list[ConversationQuality] = []
    for conversation_id in conversation_ids[:300]:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            continue
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        )
        runs = list(
            db.scalars(select(AIRun).where(AIRun.conversation_id == conversation_id))
        )
        closing = db.scalar(
            select(AIClosingState).where(AIClosingState.conversation_id == conversation_id)
        )
        ai_state = db.scalar(
            select(AIConversationState).where(
                AIConversationState.conversation_id == conversation_id
            )
        )
        followups_sent = len(
            list(
                db.scalars(
                    select(AIFollowupJob.id).where(
                        AIFollowupJob.conversation_id == conversation_id,
                        AIFollowupJob.status == "ENVIADO",
                    )
                )
            )
        )
        rows.append(
            score_conversation(
                conversation,
                messages,
                runs,
                closing=closing,
                ai_state=ai_state,
                followups_sent=followups_sent,
            )
        )

    rows.sort(key=lambda row: (row.score, -row.conversation_id))
    total = len(rows)
    total_runs = sum(row.runs for row in rows)
    summary = {
        "conversations": total,
        "average_score": round(sum(row.score for row in rows) / total) if total else 0,
        "with_repetition": sum(1 for row in rows if row.repeated_texts),
        "with_frustration": sum(1 for row in rows if row.frustration),
        "stuck": sum(1 for row in rows if row.stuck),
        "won": sum(1 for row in rows if row.outcome == "GANHO"),
        "contracts_sent": sum(1 for row in rows if row.outcome == "CONTRATO_ENVIADO"),
        "in_closing": sum(1 for row in rows if row.outcome == "FECHAMENTO"),
        "failure_rate": (
            round(100 * sum(row.failed_runs for row in rows) / total_runs) if total_runs else 0
        ),
        "guardian_block_rate": (
            round(100 * sum(row.guardian_blocks for row in rows) / total_runs) if total_runs else 0
        ),
    }
    return {
        "days": days,
        "summary": summary,
        "rows": rows,
        "guardian": guardian_observation_summary(db, days=days, conversation_ids=conversation_ids[:300]),
    }


def guardian_observation_summary(
    db: Session,
    *,
    days: int = 7,
    conversation_ids: list[int] | None = None,
) -> dict:
    """Resumo das revisões do Guardião (sombra e enforce) no período."""
    since = utcnow() - timedelta(days=max(1, days))
    logs = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "AI_SUPERVISOR_GUARDIAN_REVIEW",
                AuditLog.created_at >= since,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(3000)
        )
    )
    selected = set(conversation_ids or [])
    shadow = 0
    enforce = 0
    would_intervene = 0
    observed_blocks = 0
    issue_counts: Counter[str] = Counter()
    for row in logs:
        try:
            payload = json.loads(row.payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            conversation_id = int(payload.get("conversation_id") or 0)
        except (TypeError, ValueError):
            continue
        if selected and conversation_id not in selected:
            continue
        shadow_only = bool(payload.get("shadow_only"))
        observed = str(
            payload.get("observed_verdict") or payload.get("verdict") or "APPROVE"
        ).upper()
        if shadow_only:
            shadow += 1
        else:
            enforce += 1
        if observed != "APPROVE":
            would_intervene += 1
        if observed == "BLOCK":
            observed_blocks += 1
        issue_counts.update(
            str(item) for item in (payload.get("issue_codes") or []) if str(item)
        )
    total = shadow + enforce
    return {
        "reviews": total,
        "shadow_reviews": shadow,
        "enforce_reviews": enforce,
        "would_intervene": would_intervene,
        "would_intervene_rate": round(100 * would_intervene / total) if total else 0,
        "observed_blocks": observed_blocks,
        "top_issues": [
            {"code": code, "count": count}
            for code, count in issue_counts.most_common(8)
        ],
    }
