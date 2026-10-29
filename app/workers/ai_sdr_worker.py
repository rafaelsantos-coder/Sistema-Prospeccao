from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import AIFollowupJob, AIJob
from app.services.ai_followups import claim_followup_job, process_followup_job
from app.services.ai_sdr import claim_ai_job, process_ai_job
from app.timeutils import utcnow


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_ai_sdr_worker")


def _recover_stale_jobs() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        threshold = utcnow() - timedelta(minutes=10)
        jobs = list(
            db.scalars(
                select(AIJob).where(
                    AIJob.status == "PROCESSANDO",
                    AIJob.locked_at.is_not(None),
                    AIJob.locked_at < threshold,
                )
            )
        )
        for job in jobs:
            job.status = "PENDENTE"
            job.locked_at = None
            job.available_at = utcnow()
            job.error = "Job recuperado após timeout do worker."
            recovered += 1
        db.commit()
    finally:
        db.close()
    return recovered


def _recover_stale_followups() -> int:
    db = SessionLocal()
    recovered = 0
    try:
        threshold = utcnow() - timedelta(minutes=10)
        jobs = list(
            db.scalars(
                select(AIFollowupJob).where(
                    AIFollowupJob.status == "PROCESSANDO",
                    AIFollowupJob.locked_at.is_not(None),
                    AIFollowupJob.locked_at < threshold,
                )
            )
        )
        for job in jobs:
            job.status = "PENDENTE"
            job.locked_at = None
            job.due_at = utcnow()
            job.error = "Follow-up recuperado após timeout do worker."
            recovered += 1
        db.commit()
    finally:
        db.close()
    return recovered


def run() -> None:
    poll = max(2, int(settings.ai_sdr_poll_seconds or 3))
    LOG.info(
        "SulIA SDR worker iniciado. enabled=%s openai_configured=%s poll=%ss",
        settings.ai_sdr_enabled,
        bool(settings.openai_api_key),
        poll,
    )
    _recover_stale_jobs()
    _recover_stale_followups()

    missing_key_logged = False
    while True:
        if not settings.ai_sdr_enabled:
            time.sleep(max(10, poll))
            continue
        if not settings.openai_api_key:
            if not missing_key_logged:
                LOG.warning(
                    "OPENAI_API_KEY ausente. Shadow jobs permanecerão pendentes até a configuração da chave."
                )
                missing_key_logged = True
            time.sleep(max(15, poll))
            continue
        missing_key_logged = False

        db = SessionLocal()
        job_id = None
        followup_id = None
        try:
            job = claim_ai_job(db)
            if job:
                job_id = job.id
                db.commit()
                job = db.get(AIJob, job_id)
                if job:
                    outcome = process_ai_job(db, job)
                    db.commit()
                    LOG.info(
                        "ai_job=%s conversation=%s agent=%s outcome=%s attempts=%s",
                        job.id,
                        job.conversation_id,
                        job.agent_id,
                        outcome,
                        job.attempts,
                    )
            else:
                followup = claim_followup_job(db)
                if not followup:
                    db.commit()
                else:
                    followup_id = followup.id
                    db.commit()
                    followup = db.get(AIFollowupJob, followup_id)
                    if followup:
                        outcome = process_followup_job(db, followup)
                        db.commit()
                        LOG.info(
                            "ai_followup=%s conversation=%s step=%s outcome=%s attempts=%s",
                            followup.id,
                            followup.conversation_id,
                            followup.step,
                            outcome,
                            followup.attempts,
                        )
        except Exception:
            db.rollback()
            LOG.exception(
                "Falha inesperada no SulIA SDR worker job=%s followup=%s",
                job_id,
                followup_id,
            )
        finally:
            db.close()

        time.sleep(poll)


if __name__ == "__main__":
    run()
