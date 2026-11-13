from __future__ import annotations

from datetime import timedelta

from app.database import SessionLocal
from app.models import ProspectingJob
from app.routers.prospecting import fail_stale_jobs
from app.services.apify import _start_error
from app.timeutils import utcnow


def test_402_vira_mensagem_de_credito():
    assert "sem crédito" in _start_error(402)
    assert "HTTP 402" in _start_error(402)
    assert _start_error(500) == "Falha ao iniciar busca: HTTP 500"


def test_job_preso_e_encerrado_e_recente_nao():
    db = SessionLocal()
    try:
        old = ProspectingJob(
            segment="x", city="y", status="EXECUTANDO",
            created_at=utcnow() - timedelta(minutes=40),
        )
        new = ProspectingJob(segment="x", city="y", status="EXECUTANDO", created_at=utcnow())
        db.add_all([old, new])
        db.commit()
        assert fail_stale_jobs(db) >= 1
        db.refresh(old)
        db.refresh(new)
        assert old.status == "FALHOU" and "interrompida" in (old.error or "")
        assert new.status == "EXECUTANDO"
    finally:
        db.close()
