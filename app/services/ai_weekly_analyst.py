"""Analista semanal da SulIA.

Substitui na prática o "engenheiro IA", que gerava propostas sem acesso ao
código. Toda semana ele junta, a partir do banco:
- a qualidade das conversas (nota, repetição, frustração, paradas);
- as falhas da IA agrupadas por causa (Guardião, integridade, CRM, OpenAI);
- o efeito das regras fixas do SDR (quantas vezes cada uma reescreveu ou
  reescreveria a resposta);
- as dúvidas de clientes que a base aprovada não respondeu.

As dúvidas sem resposta viram rascunhos na base de conhecimento com status
REVIEW: não são usadas pela IA até o gestor escrever a resposta oficial e
aprovar. É assim que o manual evolui dentro de regras aprovadas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AIAgentProfile,
    AIClosingState,
    AIKnowledgeItem,
    AIKnowledgeMetadata,
    AIRun,
    AuditLog,
)
from app.services.ai_quality import conversation_quality_report
from app.services.audit import write_audit
from app.timeutils import utc_to_local, utcnow


LOG = logging.getLogger("sulnet_ai_weekly_analyst")
ANALYST_SOURCE = "Analista semanal SulIA"
RUN_ACTION = "AI_WEEKLY_ANALYST_RUN"


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def failure_cause(error: str | None) -> str:
    text = str(error or "").strip()
    folded = _fold(text)
    if not folded:
        return "Sem detalhe"
    if "guardi" in folded:
        codes = text.split(":", 1)[1].strip() if ":" in text else ""
        return f"Guardião bloqueou ({codes[:80]})" if codes else "Guardião bloqueou"
    if "integridade" in folded:
        codes = text.split(":", 1)[1].strip() if ":" in text else ""
        return f"Integridade da jornada ({codes[:80]})" if codes else "Integridade da jornada"
    if "rota nao encontrada" in folded or " 404" in folded:
        return "CRM: rota inexistente"
    if "catalogo" in folded or "catalog" in folded:
        return "CRM: plano fora do catálogo"
    if "ganho" in folded:
        return "CRM: etapa GANHO"
    if "openai" in folded or "api key" in folded or "401" in folded:
        return "OpenAI: chave ou acesso"
    if "propriedade" in folded or "ownership" in folded:
        return "Conversa mudou de dono durante a resposta"
    return text[:80]


def manual_versions(db: Session) -> list[dict]:
    """Versão de cada manual: muda sempre que um item aprovado muda."""
    versions: list[dict] = []
    agents = list(db.scalars(select(AIAgentProfile).order_by(AIAgentProfile.id)))
    for agent in agents:
        items = list(
            db.scalars(
                select(AIKnowledgeItem).where(
                    AIKnowledgeItem.agent_id == agent.id,
                    AIKnowledgeItem.active.is_(True),
                )
            )
        )
        if not items:
            versions.append({"agent": agent.name, "version": "vazio", "items": 0, "review": 0, "updated_at": None})
            continue
        statuses = {
            row.knowledge_item_id: row.approval_status
            for row in db.scalars(
                select(AIKnowledgeMetadata).where(
                    AIKnowledgeMetadata.knowledge_item_id.in_([item.id for item in items])
                )
            )
        }
        approved = [item for item in items if statuses.get(item.id, "APPROVED") == "APPROVED"]
        fingerprint = "|".join(
            f"{item.id}:{item.updated_at.isoformat() if item.updated_at else ''}"
            for item in sorted(approved, key=lambda row: row.id)
        )
        latest = max((item.updated_at for item in approved if item.updated_at), default=None)
        versions.append(
            {
                "agent": agent.name,
                "version": "v" + hashlib.sha256(fingerprint.encode()).hexdigest()[:8],
                "items": len(approved),
                "review": len(items) - len(approved),
                "updated_at": utc_to_local(latest) if latest else None,
            }
        )
    return versions


def weekly_report(db: Session, *, days: int = 7) -> dict:
    since = utcnow() - timedelta(days=max(1, days))
    runs = list(db.scalars(select(AIRun).where(AIRun.created_at >= since)))
    failed = [run for run in runs if run.status == "FALHOU"]
    causes = Counter(failure_cause(run.error) for run in failed)

    policy_effects: Counter = Counter()
    unanswered: Counter = Counter()
    for run in runs:
        try:
            output = json.loads(run.output_json or "{}")
        except (TypeError, ValueError):
            continue
        for item in output.get("policy_observations") or []:
            policy_effects[f"{item.get('policy')} ({item.get('mode')})"] += 1
        question = str(output.get("unanswered_question") or "").strip()
        if question:
            unanswered[question[:300]] += 1

    for closing in db.scalars(select(AIClosingState).where(AIClosingState.updated_at >= since)):
        try:
            collected = json.loads(closing.collected_json or "{}")
        except (TypeError, ValueError):
            continue
        for question in collected.get("_unanswered_questions") or []:
            unanswered[str(question)[:300]] += 1

    fallbacks = len(
        list(
            db.scalars(
                select(AuditLog.id).where(
                    AuditLog.action == "AI_SDR_HUMAN_FALLBACK",
                    AuditLog.created_at >= since,
                )
            )
        )
    )
    quality = conversation_quality_report(db, days=days)
    return {
        "days": days,
        "runs": len(runs),
        "failed": len(failed),
        "failure_rate": round(100 * len(failed) / len(runs)) if runs else 0,
        "causes": causes.most_common(10),
        "policy_effects": policy_effects.most_common(),
        "unanswered": unanswered.most_common(20),
        "human_fallbacks": fallbacks,
        "quality": quality,
        "worst": quality["rows"][:5],
        "manual_versions": manual_versions(db),
    }


def draft_manual_gaps(db: Session, report: dict, *, user=None) -> list[AIKnowledgeItem]:
    """Dúvidas sem resposta viram rascunhos (REVIEW) na base do SDR."""
    agent = db.scalar(select(AIAgentProfile).where(AIAgentProfile.role_type == "SDR").limit(1))
    if not agent:
        return []
    created: list[AIKnowledgeItem] = []
    for question, count in report["unanswered"]:
        title = f"Dúvida sem resposta: {question[:120]}"
        exists = db.scalar(
            select(AIKnowledgeItem.id).where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.title == title,
            )
        )
        if exists:
            continue
        item = AIKnowledgeItem(
            agent_id=agent.id,
            title=title,
            category="DUVIDAS_SEM_RESPOSTA",
            tags="pendente,analista semanal",
            content=(
                f"Clientes perguntaram ({count}x na semana): \"{question}\". "
                "A base aprovada não tinha resposta. Escreva aqui a resposta oficial da "
                "Sulnet e mude o status para Aprovado; até lá a IA não usa este item."
            ),
            source_type="ANALISTA_SEMANAL",
            active=True,
            created_by_id=getattr(user, "id", None),
        )
        db.add(item)
        db.flush()
        db.add(
            AIKnowledgeMetadata(
                knowledge_item_id=item.id,
                subject=title[:120],
                product_service="Comercial Sulnet",
                region="Área de atuação Sulnet",
                source_reference=f"{ANALYST_SOURCE} · {utc_to_local(utcnow()):%d/%m/%Y}",
                priority=60,
                approval_status="REVIEW",
            )
        )
        created.append(item)
    db.flush()
    return created


def run_weekly_analyst(db: Session, *, user=None) -> dict:
    report = weekly_report(db, days=7)
    drafts = draft_manual_gaps(db, report, user=user)
    week = utc_to_local(utcnow()).strftime("%G-W%V")
    write_audit(
        db,
        user=user,
        action=RUN_ACTION,
        entity_type="ai",
        entity_id=week,
        payload={
            "week": week,
            "runs": report["runs"],
            "failure_rate": report["failure_rate"],
            "causes": report["causes"][:5],
            "average_score": report["quality"]["summary"]["average_score"],
            "drafts_created": len(drafts),
        },
    )
    return {"week": week, "drafts_created": len(drafts), "report": report}


def _already_ran_this_week(db: Session, week: str) -> bool:
    return bool(
        db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == RUN_ACTION,
                AuditLog.entity_id == week,
            )
        )
    )


def run_weekly_analyst_loop() -> None:
    """Roda uma vez por semana, segunda-feira a partir das 8h (horário local)."""
    LOG.info("Analista semanal SulIA iniciado.")
    while True:
        local_now = utc_to_local(utcnow())
        week = local_now.strftime("%G-W%V")
        if local_now.weekday() == 0 and local_now.hour >= 8:
            db = SessionLocal()
            try:
                if not _already_ran_this_week(db, week):
                    result = run_weekly_analyst(db)
                    db.commit()
                    LOG.info(
                        "ai_weekly_analyst week=%s drafts=%s",
                        result["week"],
                        result["drafts_created"],
                    )
            except Exception:
                db.rollback()
                LOG.exception("Falha no analista semanal.")
            finally:
                db.close()
        time.sleep(1800)
