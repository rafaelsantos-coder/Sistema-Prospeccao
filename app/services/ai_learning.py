from __future__ import annotations

import hashlib
import logging
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AIConversationState,
    AIExperiment,
    AIExperimentAssignment,
    Conversation,
)
from app.services.audit import write_audit
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_learning")
LEARNING_ACTION = "AI_LEARNING_EXPERIMENT"

_SAFE_EXPERIMENTS = (
    {
        "key": "sdr_plain_segment_question_v1",
        "scope": "SDR_COPY",
        "hypothesis": (
            "Perguntas em linguagem cotidiana reduzem abandono sem mudar a regra comercial."
        ),
        "control_instruction": "Mantenha a redação comercial atual aprovada.",
        "variant_instruction": (
            "Somente na redação, use linguagem cotidiana e curta. Para segmento, prefira "
            "'A internet é para sua casa ou empresa?' em vez de termos técnicos. "
            "Não altere preço, plano, condição, ordem da jornada ou critério de handoff."
        ),
        "metric": "CLOSING_RATE",
    },
    {
        "key": "followup_context_question_v1",
        "scope": "FOLLOWUP_COPY",
        "hypothesis": (
            "Follow-up curto que retoma o contexto e termina em uma pergunta objetiva "
            "gera mais continuidade que mensagens genéricas."
        ),
        "control_instruction": "Mantenha a redação atual de follow-up.",
        "variant_instruction": (
            "Somente na redação do follow-up: retome em uma frase o ponto exato onde a "
            "conversa parou e termine com uma única pergunta curta e objetiva. Evite "
            "perguntas genéricas como 'ainda tem interesse?'. Não altere oferta ou regra."
        ),
        "metric": "CLOSING_RATE",
    },
    {
        "key": "sdr_alternative_close_v1",
        "scope": "SDR_COPY",
        "hypothesis": (
            "Convite de fechamento com duas alternativas que avançam fecha mais do que "
            "a pergunta de sim ou não."
        ),
        "control_instruction": "Mantenha o convite de fechamento atual aprovado.",
        "variant_instruction": (
            "Somente na redação do convite de fechamento: quando houver duas opções válidas "
            "para o cliente (dois planos elegíveis ou dois próximos passos), ofereça as duas "
            "em vez de perguntar 'quer contratar?'. Não altere preço, plano, condição, ordem "
            "da jornada ou critério de handoff."
        ),
        "metric": "CLOSING_RATE",
    },
    {
        "key": "sdr_acknowledge_before_offer_v1",
        "scope": "SDR_COPY",
        "hypothesis": (
            "Retomar em poucas palavras o que o cliente disse antes da oferta aumenta a "
            "continuidade da conversa."
        ),
        "control_instruction": "Mantenha a redação comercial atual aprovada.",
        "variant_instruction": (
            "Somente na redação: comece a resposta retomando em poucas palavras o que o "
            "cliente acabou de dizer e só então faça a pergunta ou a oferta. Não altere "
            "preço, plano, condição, ordem da jornada ou critério de handoff."
        ),
        "metric": "CLOSING_RATE",
    },
)


def _seed_experiments(db: Session) -> int:
    created = 0
    for spec in _SAFE_EXPERIMENTS:
        exists = db.scalar(
            select(AIExperiment.id).where(AIExperiment.key == spec["key"]).limit(1)
        )
        if exists:
            continue
        db.add(
            AIExperiment(
                key=spec["key"],
                scope=spec["scope"],
                status="ACTIVE",
                hypothesis=spec["hypothesis"],
                control_instruction=spec["control_instruction"],
                variant_instruction=spec["variant_instruction"],
                metric=spec["metric"],
                source_summary="Experimento seguro pré-aprovado pela arquitetura da Fase D.",
                started_at=utcnow(),
            )
        )
        created += 1
    return created


def _stable_variant(experiment_id: int, conversation_id: int) -> str:
    digest = hashlib.sha256(
        f"{experiment_id}:{conversation_id}".encode("utf-8")
    ).digest()
    return "VARIANT" if digest[0] % 2 else "CONTROL"


def _assignment(
    db: Session,
    *,
    experiment: AIExperiment,
    conversation_id: int,
) -> AIExperimentAssignment:
    row = db.scalar(
        select(AIExperimentAssignment).where(
            AIExperimentAssignment.experiment_id == experiment.id,
            AIExperimentAssignment.conversation_id == conversation_id,
        )
    )
    if row:
        return row
    row = AIExperimentAssignment(
        experiment_id=experiment.id,
        conversation_id=conversation_id,
        variant=_stable_variant(experiment.id, conversation_id),
    )
    db.add(row)
    db.flush()
    return row


def learning_context_for_conversation(
    db: Session,
    *,
    conversation_id: int,
    scope: str,
) -> dict:
    if not settings.ai_learning_enabled:
        return {}

    promoted = db.scalar(
        select(AIExperiment)
        .where(
            AIExperiment.scope == scope,
            AIExperiment.status == "PROMOTED",
        )
        .order_by(AIExperiment.completed_at.desc(), AIExperiment.id.desc())
        .limit(1)
    )
    active = db.scalar(
        select(AIExperiment)
        .where(
            AIExperiment.scope == scope,
            AIExperiment.status == "ACTIVE",
        )
        .order_by(AIExperiment.id.asc())
        .limit(1)
    )

    result: dict[str, object] = {
        "scope": scope,
        "baseline_instruction": "",
        "experiment_instruction": "",
        "experiment_id": None,
        "variant": "",
    }
    if promoted and promoted.winner == "VARIANT":
        result["baseline_instruction"] = promoted.variant_instruction

    if active:
        assignment = _assignment(
            db,
            experiment=active,
            conversation_id=conversation_id,
        )
        result["experiment_id"] = active.id
        result["variant"] = assignment.variant
        result["experiment_instruction"] = (
            active.variant_instruction
            if assignment.variant == "VARIANT"
            else active.control_instruction
        )
        result["hypothesis"] = active.hypothesis
    return result


def _conversation_converted(db: Session, conversation_id: int) -> bool:
    state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation_id
        )
    )
    if state and state.state in {"CLOSING", "COMPLETED"}:
        return True
    conversation = db.get(Conversation, conversation_id)
    return bool(
        conversation
        and conversation.lead
        and conversation.lead.stage == "GANHO"
    )


def _refresh_experiment(db: Session, experiment: AIExperiment) -> None:
    assignments = list(
        db.scalars(
            select(AIExperimentAssignment).where(
                AIExperimentAssignment.experiment_id == experiment.id
            )
        )
    )
    for assignment in assignments:
        if assignment.converted:
            continue
        if _conversation_converted(db, assignment.conversation_id):
            assignment.converted = True
            assignment.converted_at = utcnow()

    control = [item for item in assignments if item.variant == "CONTROL"]
    variant = [item for item in assignments if item.variant == "VARIANT"]
    control_conversions = sum(1 for item in control if item.converted)
    variant_conversions = sum(1 for item in variant if item.converted)

    experiment.control_samples = len(control)
    experiment.variant_samples = len(variant)
    experiment.control_conversions = control_conversions
    experiment.variant_conversions = variant_conversions

    min_samples = max(10, int(settings.ai_learning_min_samples_per_arm or 30))
    if len(control) < min_samples or len(variant) < min_samples:
        return

    control_rate = control_conversions / max(1, len(control))
    variant_rate = variant_conversions / max(1, len(variant))
    uplift = variant_rate - control_rate
    experiment.confidence = min(0.99, abs(uplift) * 5 + 0.5)

    threshold = max(0.01, float(settings.ai_learning_min_uplift or 0.05))
    winner = ""
    if uplift >= threshold:
        winner = "VARIANT"
    elif uplift <= -threshold:
        winner = "CONTROL"

    if not winner:
        return

    experiment.status = "PROMOTED"
    experiment.winner = winner
    experiment.completed_at = utcnow()
    write_audit(
        db,
        user=None,
        action=LEARNING_ACTION,
        entity_type="ai_experiment",
        entity_id=experiment.id,
        payload={
            "key": experiment.key,
            "scope": experiment.scope,
            "winner": winner,
            "control_samples": len(control),
            "variant_samples": len(variant),
            "control_rate": control_rate,
            "variant_rate": variant_rate,
            "uplift": uplift,
        },
    )


def run_learning_cycle(db: Session) -> dict[str, int]:
    if not settings.ai_learning_enabled:
        return {"created": 0, "evaluated": 0, "promoted": 0}

    created = _seed_experiments(db)
    active = list(
        db.scalars(
            select(AIExperiment).where(AIExperiment.status == "ACTIVE")
        )
    )
    promoted_before = sum(
        1
        for item in active
        if item.status == "PROMOTED"
    )
    for experiment in active:
        _refresh_experiment(db, experiment)
    promoted_after = sum(1 for item in active if item.status == "PROMOTED")
    return {
        "created": created,
        "evaluated": len(active),
        "promoted": max(0, promoted_after - promoted_before),
    }


def run_learning_loop() -> None:
    poll = max(60, int(settings.ai_learning_poll_seconds or 300))
    LOG.info("SulIA learning iniciado. enabled=%s poll=%ss", settings.ai_learning_enabled, poll)
    while True:
        if settings.ai_learning_enabled:
            db = SessionLocal()
            try:
                result = run_learning_cycle(db)
                db.commit()
                if any(result.values()):
                    LOG.info("ai_learning_cycle=%s", result)
            except Exception:
                db.rollback()
                LOG.exception("Falha no ciclo de aprendizado/experimentos.")
            finally:
                db.close()
        time.sleep(poll)
