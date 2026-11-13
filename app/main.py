from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, validate_production_settings
from app.database import Base, SessionLocal, engine
from app.routers import credit_map
from app.routers import ai_agents, api, auth, campaigns, conversations, dashboard, diagnostico, dispatches, leads, presence, prospecting, queues, reports, settings as settings_router, telegram as telegram_router, zapi, zapsign
from app.services.seed import seed_database
from app.services.zapi import ZApiError
from app.services.zapi_instances import instance_webhook_url, list_zapi_instances, service_for_instance
from app.security_middleware import install_security_middlewares
from app.redis_session import RedisSessionMiddleware
from app.web import render


LOG = logging.getLogger("uvicorn.error")


class _RedactWebhookSecretFilter(logging.Filter):
    """Mascara o segredo que vai na URL dos webhooks (/webhooks/<tipo>/<segredo>) no log de acesso."""

    _PATTERN = re.compile(r"(/webhooks/[^/\s?]+/(?:instance/)?)[^/\s?]+")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "/webhooks/" in args[2]:
            record.args = (*args[:2], self._PATTERN.sub(r"\1***", args[2]), *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactWebhookSecretFilter())


async def initialize_database(max_attempts: int = 12) -> None:
    """Cria o schema e dados-base, tolerando o tempo de subida do Postgres no Railway."""
    for attempt in range(1, max_attempts + 1):
        try:
            if not settings.is_production:
                Base.metadata.create_all(bind=engine)
            db = SessionLocal()
            try:
                seed_database(db, include_demo=not settings.is_production)
            finally:
                db.close()
            return
        except OperationalError:
            if attempt == max_attempts:
                raise
            delay = min(2 * attempt, 15)
            LOG.warning(
                "Banco ainda indisponível; nova tentativa em %ss (%s/%s).",
                delay,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(delay)


async def configure_zapi_webhooks() -> None:
    if not settings.app_public_url:
        return
    db = SessionLocal()
    try:
        instances = list_zapi_instances(db)
        for instance in instances:
            webhook_url = instance_webhook_url(instance)
            if not webhook_url:
                continue
            try:
                result = await asyncio.to_thread(
                    service_for_instance(instance).configure_webhooks,
                    webhook_url,
                )
            except ZApiError as exc:
                LOG.warning(
                    "Z-API %s configurada, mas o webhook nao foi atualizado: %s",
                    instance.name,
                    exc,
                )
            else:
                state = result.get("webhook_state") or {}
                LOG.info(
                    "Webhooks da Z-API %s configurados. received_ok=%s combined_ok=%s sent_by_me=%s connected=%s",
                    instance.name,
                    bool(state.get("received_callback_url")),
                    bool(state.get("received_and_delivery_callback_url")),
                    bool(state.get("receive_sent_by_me")),
                    bool(state.get("connected")),
                )
    finally:
        db.close()


def log_recent_prospecting_jobs() -> None:
    """Registra os últimos jobs de prospecção (sem dados pessoais) para diagnóstico por log."""
    from sqlalchemy import select

    from app.models import ProspectingJob

    from app.routers.prospecting import fail_stale_jobs

    db = SessionLocal()
    try:
        fail_stale_jobs(db)
        for job in db.scalars(
            select(ProspectingJob).order_by(ProspectingJob.id.desc()).limit(10)
        ):
            LOG.info(
                "prospecting_report job=%s status=%s found=%s imported=%s created=%s "
                "started=%s finished=%s error=%s plan=%s",
                job.id,
                job.status,
                job.found_count,
                job.imported_count,
                job.created_at,
                job.started_at,
                job.finished_at,
                (job.error or "-")[:300],
                (job.execution_json or "{}")[:300],
            )
    except Exception:  # diagnóstico nunca pode impedir a subida
        LOG.warning("prospecting_report_failed", exc_info=True)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_production_settings()
    await initialize_database()
    log_recent_prospecting_jobs()
    await configure_zapi_webhooks()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.9.1",
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
    lifespan=lifespan,
)

if settings.redis_url:
    app.add_middleware(
        RedisSessionMiddleware,
        redis_url=settings.redis_url,
        secret_key=settings.effective_session_signing_key,
        cookie_name=settings.session_cookie,
        same_site="lax",
        https_only=settings.is_production,
        max_age=settings.session_max_age_seconds,
    )
else:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.effective_session_signing_key,
        session_cookie=settings.session_cookie,
        same_site="lax",
        https_only=settings.is_production,
        max_age=settings.session_max_age_seconds,
    )

install_security_middlewares(app)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(ai_agents.router)
app.include_router(dashboard.router)
app.include_router(leads.router)
app.include_router(campaigns.router)
app.include_router(dispatches.router)
app.include_router(conversations.router)
app.include_router(prospecting.router)
app.include_router(reports.router)
app.include_router(credit_map.router)
app.include_router(presence.router)
app.include_router(queues.router)
app.include_router(settings_router.router)
app.include_router(zapi.router)
app.include_router(telegram_router.router)
app.include_router(zapsign.router)
app.include_router(api.router)
app.include_router(diagnostico.router)


@app.exception_handler(404)
async def not_found(request: Request, _exc):
    return render(request, "404.html", page_title="Página não encontrada", status_code=404)
