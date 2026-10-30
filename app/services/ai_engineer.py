from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AIEngineeringProposal, AuditLog
from app.services.audit import write_audit
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_engineer")
ENGINEER_ACTION = "AI_ENGINEER_PROPOSAL"

_SAFE_CODES = {
    "POSSIBLE_REPEAT",
    "STATE_REGRESSION",
    "IGNORED_CONFIRMED_ANSWER",
    "MISSING_QUICK_REPLIES",
    "WRONG_STATE_PROGRESSION",
}
_ALLOWED_PATHS = {
    "app/services/ai_sdr.py",
    "app/services/ai_guardian.py",
    "app/services/ai_supervisor.py",
    "app/services/ai_followups.py",
    "app/services/ai_quick_replies.py",
    "tests/test_ai_sdr.py",
    "tests/test_ai_guardian.py",
    "tests/test_ai_supervisor.py",
}
_FORBIDDEN_PATCH_TERMS = (
    "sulnet_crm_bearer_token",
    "sulnet_ai_integration_key",
    "secret_key",
    "password",
    "credit_status",
    "contract_status",
    "offer_price",
    "price_with_fidelity",
    "mobile_plans",
    "ftth_plans",
    "corporate_plans",
)


class EngineerReplacement(BaseModel):
    path: str
    old_text: str = Field(min_length=1, max_length=6000)
    new_text: str = Field(min_length=1, max_length=6000)
    reason: str = ""


class EngineerPatchPlan(BaseModel):
    title: str = Field(max_length=220)
    diagnosis: str = Field(max_length=3000)
    risk: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    replacements: list[EngineerReplacement] = Field(default_factory=list, max_length=4)
    test_replacements: list[EngineerReplacement] = Field(default_factory=list, max_length=3)
    test_plan: list[str] = Field(default_factory=list, max_length=8)


def _safe_json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _incident_codes(payload: dict) -> list[str]:
    values = payload.get("issue_codes") or []
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if str(item) in _SAFE_CODES]


def _incident_summary(db: Session, *, hours: int = 12) -> tuple[str, list[dict]]:
    threshold = utcnow() - timedelta(hours=max(1, hours))
    rows = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action.in_(
                    [
                        "AI_SUPERVISOR_SHADOW_REVIEW",
                        "AI_SUPERVISOR_GUARDIAN_REVIEW",
                    ]
                ),
                AuditLog.created_at >= threshold,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(500)
        )
    )
    evidence: list[dict] = []
    counts: Counter[str] = Counter()
    for row in rows:
        payload = _safe_json(row.payload_json)
        codes = _incident_codes(payload)
        for code in codes:
            counts[code] += 1
        if codes and len(evidence) < 30:
            evidence.append(
                {
                    "audit_id": row.id,
                    "action": row.action,
                    "entity_id": row.entity_id,
                    "codes": codes,
                    "verdict": payload.get("verdict"),
                    "rationale": str(payload.get("rationale") or "")[:500],
                }
            )

    eligible = [
        (code, count)
        for code, count in counts.most_common()
        if count >= max(2, int(settings.ai_engineer_min_incidents or 3))
    ]
    if not eligible:
        return "", evidence
    fingerprint = "|".join(code for code, _count in eligible[:5])
    return fingerprint, evidence


def _existing_open_proposal(
    db: Session,
    fingerprint: str,
) -> AIEngineeringProposal | None:
    return db.scalar(
        select(AIEngineeringProposal)
        .where(
            AIEngineeringProposal.fingerprint == fingerprint,
            AIEngineeringProposal.status.in_(["PROPOSED", "PR_OPEN", "CI_RUNNING"]),
        )
        .order_by(AIEngineeringProposal.id.desc())
        .limit(1)
    )


def _github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.ai_engineer_github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_api(path: str) -> str:
    return (
        f"https://api.github.com/repos/"
        f"{settings.ai_engineer_github_repository}{path}"
    )


def _github_get_file(
    client: httpx.Client,
    path: str,
    ref: str,
) -> tuple[str, str]:
    response = client.get(
        _github_api(f"/contents/{path}"),
        params={"ref": ref},
        headers=_github_headers(),
    )
    response.raise_for_status()
    data = response.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, str(data["sha"])


def _excerpt_for_terms(content: str, terms: tuple[str, ...]) -> str:
    lines = content.splitlines()
    selected: set[int] = set()
    lowered = [line.casefold() for line in lines]
    for index, line in enumerate(lowered):
        if any(term.casefold() in line for term in terms):
            for offset in range(
                max(0, index - 35),
                min(len(lines), index + 55),
            ):
                selected.add(offset)
    if not selected:
        return "\n".join(lines[:180])
    return "\n".join(
        f"{index + 1}: {lines[index]}"
        for index in sorted(selected)
    )[:14000]


def _engineer_source_context(fingerprint: str) -> dict[str, str]:
    if not settings.ai_engineer_github_token:
        return {}

    codes = {
        part.split(":", 1)[0]
        for part in fingerprint.split("|")
        if ":" in part
    }
    targets: list[tuple[str, tuple[str, ...]]] = []

    if codes & {
        "POSSIBLE_REPEAT",
        "STATE_REGRESSION",
        "IGNORED_CONFIRMED_ANSWER",
        "WRONG_STATE_PROGRESSION",
    }:
        targets.extend(
            [
                (
                    "app/services/ai_sdr.py",
                    (
                        "_journey_integrity_issues",
                        "_sdr_reply_repeats_recent_outbound",
                        "_derive_journey_state",
                    ),
                ),
                (
                    "app/services/ai_supervisor.py",
                    (
                        "_deterministic_findings",
                        "STATE_REGRESSION",
                        "POSSIBLE_REPEAT",
                    ),
                ),
                (
                    "tests/test_ai_sdr.py",
                    ("integrity", "regress", "repeat"),
                ),
            ]
        )

    if "MISSING_QUICK_REPLIES" in codes:
        targets.extend(
            [
                (
                    "app/services/ai_quick_replies.py",
                    ("quick_replies_for_message", "button"),
                ),
                (
                    "app/services/ai_guardian.py",
                    ("MISSING_QUICK_REPLIES", "_normalize_buttons"),
                ),
                (
                    "tests/test_ai_guardian.py",
                    ("quick", "button"),
                ),
            ]
        )

    context: dict[str, str] = {}
    seen: set[str] = set()
    with httpx.Client(timeout=30.0) as client:
        for path, terms in targets[:6]:
            if path in seen or path not in _ALLOWED_PATHS:
                continue
            seen.add(path)
            try:
                source, _sha = _github_get_file(client, path, "main")
            except Exception:
                LOG.exception("Engineer IA não conseguiu ler %s", path)
                continue
            context[path] = _excerpt_for_terms(source, terms)
    return context


def _run_engineer_model(
    fingerprint: str,
    evidence: list[dict],
    source_context: dict[str, str] | None = None,
) -> EngineerPatchPlan:
    from agents import Agent, Runner

    prompt = f"""
Você é o Engineer IA do Sulnet Prospecta.
Diagnostique recorrências operacionais do SDR/Supervisor e proponha uma correção
pequena, reversível e coberta por testes.

ZONA PERMITIDA:
- ai_sdr.py
- ai_guardian.py
- ai_supervisor.py
- ai_followups.py
- ai_quick_replies.py
- testes correspondentes.

PROIBIDO:
- catálogo, preços, contratos, crédito;
- credenciais, banco, migrações e segurança;
- CRM, integrações e qualquer ampliação de autonomia comercial.

Se TRECHOS DE CÓDIGO AUTORIZADOS estiver vazio, não gere replacements.
Quando houver trechos, old_text precisa ser cópia exata de um trecho fornecido e
new_text deve ser a menor correção possível. Inclua teste de regressão sempre que
houver patch.

FINGERPRINT:
{fingerprint}

EVIDÊNCIAS:
{json.dumps(evidence, ensure_ascii=False, default=str)}

TRECHOS DE CÓDIGO AUTORIZADOS:
{json.dumps(source_context or {}, ensure_ascii=False, default=str)}
""".strip()

    agent = Agent(
        name="SulIA Engineer",
        instructions=(
            "Trabalhe como engenheiro de confiabilidade. Mudança mínima, "
            "teste primeiro, sem ampliar autonomia comercial."
        ),
        model=settings.ai_supervisor_model,
        model_settings={
            "reasoning": {"effort": "medium"},
            "verbosity": "low",
            "timeout": 45.0,
        },
        output_type=EngineerPatchPlan,
    )
    result = Runner.run_sync(agent, prompt)
    value = result.final_output
    if isinstance(value, EngineerPatchPlan):
        return value
    return EngineerPatchPlan.model_validate(value)


def _path_allowed(path: str) -> bool:
    return path in _ALLOWED_PATHS


def _replacement_safe(change: EngineerReplacement) -> bool:
    if not _path_allowed(change.path):
        return False
    combined = f"{change.old_text}\n{change.new_text}".casefold()
    if any(term in combined for term in _FORBIDDEN_PATCH_TERMS):
        return False
    if re.search(r"r\$\s*\d|\b\d{2,4}[,.]\d{2}\b", combined):
        return False
    return True


def _github_create_branch(client: httpx.Client, branch: str) -> None:
    ref = client.get(
        _github_api("/git/ref/heads/main"),
        headers=_github_headers(),
    )
    ref.raise_for_status()
    sha = ref.json()["object"]["sha"]
    response = client.post(
        _github_api("/git/refs"),
        headers=_github_headers(),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    response.raise_for_status()


def _github_update_file(
    client: httpx.Client,
    *,
    branch: str,
    path: str,
    content: str,
    sha: str,
    message: str,
) -> None:
    response = client.put(
        _github_api(f"/contents/{path}"),
        headers=_github_headers(),
        json={
            "message": message,
            "content": base64.b64encode(
                content.encode("utf-8")
            ).decode("ascii"),
            "sha": sha,
            "branch": branch,
        },
    )
    response.raise_for_status()


def _open_patch_pr(
    proposal: AIEngineeringProposal,
    plan: EngineerPatchPlan,
) -> str:
    changes = [
        item
        for item in (plan.replacements + plan.test_replacements)
        if _replacement_safe(item)
    ]
    if not changes:
        return ""

    branch = (
        f"ai-engineer/{proposal.id}-"
        f"{hashlib.sha256(proposal.fingerprint.encode()).hexdigest()[:8]}"
    )
    with httpx.Client(timeout=30.0) as client:
        _github_create_branch(client, branch)
        for change in changes:
            current, sha = _github_get_file(client, change.path, branch)
            if current.count(change.old_text) != 1:
                raise RuntimeError(
                    "Engineer IA não encontrou substituição única em "
                    f"{change.path}."
                )
            updated = current.replace(
                change.old_text,
                change.new_text,
                1,
            )
            _github_update_file(
                client,
                branch=branch,
                path=change.path,
                content=updated,
                sha=sha,
                message=f"fix(ai-engineer): {plan.title[:120]}",
            )

        response = client.post(
            _github_api("/pulls"),
            headers=_github_headers(),
            json={
                "title": f"fix(ai-engineer): {plan.title[:180]}",
                "head": branch,
                "base": "main",
                "body": (
                    "PR gerado pelo Engineer IA em zona segura.\n\n"
                    f"Diagnóstico: {plan.diagnosis}\n\n"
                    "CI obrigatório; sem auto-merge e sem alteração de preço, "
                    "catálogo, contrato, crédito, credenciais ou migrações."
                ),
            },
        )
        response.raise_for_status()
        data = response.json()
        proposal.branch_name = branch
        proposal.pull_request_url = str(data.get("html_url") or "")
        proposal.status = "PR_OPEN"
        return proposal.pull_request_url


def run_engineer_cycle(db: Session) -> dict[str, int]:
    if not settings.ai_engineer_enabled:
        return {"proposed": 0, "pr_opened": 0}

    fingerprint, evidence = _incident_summary(db)
    if not fingerprint or _existing_open_proposal(db, fingerprint):
        return {"proposed": 0, "pr_opened": 0}

    source_context = {}
    if (
        settings.ai_engineer_autonomous_pr_enabled
        and settings.ai_engineer_github_token
    ):
        source_context = _engineer_source_context(fingerprint)

    plan = _run_engineer_model(
        fingerprint,
        evidence,
        source_context,
    )
    proposal = AIEngineeringProposal(
        fingerprint=fingerprint,
        status="PROPOSED",
        risk=plan.risk,
        title=plan.title,
        diagnosis=plan.diagnosis,
        patch_plan_json=json.dumps(
            [item.model_dump() for item in plan.replacements],
            ensure_ascii=False,
        ),
        test_plan_json=json.dumps(
            {
                "replacements": [
                    item.model_dump()
                    for item in plan.test_replacements
                ],
                "checks": plan.test_plan,
            },
            ensure_ascii=False,
        ),
        evidence_json=json.dumps(
            evidence,
            ensure_ascii=False,
            default=str,
        ),
    )
    db.add(proposal)
    db.flush()

    opened = 0
    if (
        settings.ai_engineer_autonomous_pr_enabled
        and settings.ai_engineer_github_token
        and settings.ai_engineer_github_repository
        and plan.risk in {"LOW", "MEDIUM"}
    ):
        try:
            if _open_patch_pr(proposal, plan):
                opened = 1
        except Exception as exc:
            proposal.error = str(exc)[:2000]
            LOG.exception(
                "Engineer IA falhou ao abrir PR proposal=%s",
                proposal.id,
            )

    write_audit(
        db,
        user=None,
        action=ENGINEER_ACTION,
        entity_type="ai_engineering_proposal",
        entity_id=proposal.id,
        payload={
            "fingerprint": fingerprint,
            "risk": proposal.risk,
            "title": proposal.title,
            "status": proposal.status,
            "pull_request_url": proposal.pull_request_url,
        },
    )
    return {"proposed": 1, "pr_opened": opened}


def run_engineer_loop() -> None:
    poll = max(120, int(settings.ai_engineer_poll_seconds or 300))
    LOG.info(
        "SulIA Engineer iniciado. enabled=%s autonomous_pr=%s poll=%ss",
        settings.ai_engineer_enabled,
        settings.ai_engineer_autonomous_pr_enabled,
        poll,
    )
    while True:
        if settings.ai_engineer_enabled:
            db = SessionLocal()
            try:
                result = run_engineer_cycle(db)
                db.commit()
                if any(result.values()):
                    LOG.info("ai_engineer_cycle=%s", result)
            except Exception:
                db.rollback()
                LOG.exception("Falha no ciclo do Engineer IA.")
            finally:
                db.close()
        time.sleep(poll)
