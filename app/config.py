from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def normalize_database_url(value: str) -> str:
    """Normaliza a URL do Railway para o driver psycopg 3 usado pelo projeto."""
    value = value.strip()
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Sulnet Prospecta")
    app_env: str = os.getenv("APP_ENV", "development")
    app_public_url: str = os.getenv("APP_PUBLIC_URL", "").rstrip("/")
    secret_key: str = os.getenv("SECRET_KEY", "dev-only-change-this-secret")
    database_url: str = normalize_database_url(
        os.getenv(
            "DATABASE_URL",
            f"sqlite:///{(BASE_DIR / 'data' / 'sulnet_prospecta.db').as_posix()}",
        )
    )
    session_cookie: str = os.getenv("SESSION_COOKIE", "sulnet_prospecta_session")
    bootstrap_admin_name: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Administrador Sulnet")
    bootstrap_admin_email: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@sulnet.local")
    bootstrap_admin_password: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "Sulnet@2026!")
    apify_token: str = os.getenv("APIFY_TOKEN", "")
    apify_actor_id: str = os.getenv("APIFY_ACTOR_ID", "nwua9Gu5YrADL7ZDj")
    zapi_base_url: str = os.getenv("ZAPI_BASE_URL", "https://api.z-api.io")
    zapi_instance_id: str = os.getenv("ZAPI_INSTANCE_ID", "")
    zapi_instance_token: str = os.getenv("ZAPI_INSTANCE_TOKEN", "")
    zapi_client_token: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
    zapi_webhook_secret: str = os.getenv("ZAPI_WEBHOOK_SECRET", "")
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    resend_from_email: str = os.getenv("RESEND_FROM_EMAIL", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    ai_sdr_enabled: bool = os.getenv("AI_SDR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    ai_autonomous_enabled: bool = os.getenv("AI_AUTONOMOUS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    ai_sdr_poll_seconds: int = int(os.getenv("AI_SDR_POLL_SECONDS", "3"))
    media_s3_endpoint: str = os.getenv("MEDIA_S3_ENDPOINT", "")
    media_s3_bucket: str = os.getenv("MEDIA_S3_BUCKET", "")
    media_s3_region: str = os.getenv("MEDIA_S3_REGION", "auto")
    media_s3_access_key_id: str = os.getenv("MEDIA_S3_ACCESS_KEY_ID", "")
    media_s3_secret_access_key: str = os.getenv("MEDIA_S3_SECRET_ACCESS_KEY", "")
    media_s3_url_style: str = os.getenv("MEDIA_S3_URL_STYLE", "virtual-host")
    media_max_upload_mb: int = int(os.getenv("MEDIA_MAX_UPLOAD_MB", "50"))
    whatsapp_presence_timeout_seconds: int = int(os.getenv("WHATSAPP_PRESENCE_TIMEOUT_SECONDS", "90"))
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def zapi_webhook_url(self) -> str:
        if not self.app_public_url or not self.zapi_webhook_secret:
            return ""
        return f"{self.app_public_url}/webhooks/zapi/{self.zapi_webhook_secret}"

    @property
    def integrations(self) -> dict[str, bool]:
        zapi_configured = bool(self.zapi_instance_id and self.zapi_instance_token)
        return {
            "apify": bool(self.apify_token),
            "zapi": zapi_configured,
            "whatsapp": zapi_configured,
            "email": bool(self.resend_api_key and self.resend_from_email),
            "openai": bool(self.openai_api_key),
            "ai_sdr": bool(self.ai_sdr_enabled and self.openai_api_key),
        }


settings = Settings()


def validate_production_settings() -> None:
    if not settings.is_production:
        return
    errors: list[str] = []
    if settings.secret_key == "dev-only-change-this-secret" or len(settings.secret_key) < 32:
        errors.append("SECRET_KEY deve ter ao menos 32 caracteres e não pode usar o valor padrão")
    if settings.bootstrap_admin_password in {"Sulnet@2026!", "troque-esta-senha"}:
        errors.append("BOOTSTRAP_ADMIN_PASSWORD deve ser alterada")
    if len(settings.bootstrap_admin_password) < 12:
        errors.append("BOOTSTRAP_ADMIN_PASSWORD deve ter ao menos 12 caracteres")
    if not settings.database_url.startswith("postgresql+psycopg://"):
        errors.append("DATABASE_URL deve apontar para PostgreSQL em produção")
    zapi_values = [settings.zapi_instance_id, settings.zapi_instance_token]
    if any(zapi_values) and not all(zapi_values):
        errors.append("ZAPI_INSTANCE_ID e ZAPI_INSTANCE_TOKEN devem ser configurados juntos")
    if all(zapi_values):
        if not settings.app_public_url.startswith("https://"):
            errors.append("APP_PUBLIC_URL deve usar HTTPS quando a Z-API estiver configurada")
        if len(settings.zapi_webhook_secret) < 24:
            errors.append("ZAPI_WEBHOOK_SECRET deve ter ao menos 24 caracteres")
    if errors:
        raise RuntimeError("Configuração de produção inválida: " + "; ".join(errors))
