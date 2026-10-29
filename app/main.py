from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, validate_production_settings
from app.database import Base, SessionLocal, engine
from app.routers import ai_agents, api, auth, campaigns, conversations, dashboard, dispatches, leads, presence, prospecting, queues, reports, settings as settings_router, zapi
from app.services.seed import seed_database
from app.services.zapi import ZApiError
from app.services.zapi_instances import instance_webhook_url, list_zapi_instances, service_for_instance
from app.web import render


LOG = logging.getLogger("uvicorn.error")


async def initialize_database(max_attempts: int = 12) -> None:
    """Cria o schema e dados-base, tolerando o tempo de subida do Postgres no Railway."""
    for attempt in range(1, max_attempts + 1):
        try:
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_production_settings()
    await initialize_database()
    await configure_zapi_webhooks()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.9.1",
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    same_site="lax",
    https_only=settings.is_production,
    max_age=60 * 60 * 12,
)

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
app.include_router(presence.router)
app.include_router(queues.router)
app.include_router(settings_router.router)
app.include_router(zapi.router)
app.include_router(api.router)


@app.exception_handler(404)
async def not_found(request: Request, _exc):
    return render(request, "404.html", page_title="Página não encontrada", status_code=404)
