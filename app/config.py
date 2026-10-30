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
    session_signing_key: str = os.getenv("SESSION_SIGNING_KEY", "").strip()
    data_encryption_key: str = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    legacy_data_encryption_key: str = os.getenv("LEGACY_DATA_ENCRYPTION_KEY", "").strip()
    database_pool_size: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    database_max_overflow: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "10"))
    database_pool_timeout_seconds: int = int(os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "15"))
    database_pool_recycle_seconds: int = int(os.getenv("DATABASE_POOL_RECYCLE_SECONDS", "1800"))
    database_url: str = normalize_database_url(
        os.getenv(
            "DATABASE_URL",
            f"sqlite:///{(BASE_DIR / 'data' / 'sulnet_prospecta.db').as_posix()}",
        )
    )
    session_cookie: str = os.getenv("SESSION_COOKIE", "sulnet_prospecta_session")
    redis_url: str = os.getenv("REDIS_URL", "").strip()
    session_max_age_seconds: int = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 12)))
    login_max_attempts: int = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
    login_window_seconds: int = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))
    login_block_seconds: int = int(os.getenv("LOGIN_BLOCK_SECONDS", "900"))
    mfa_required_roles: str = os.getenv("MFA_REQUIRED_ROLES", "").strip()
    oidc_metadata_url: str = os.getenv("OIDC_METADATA_URL", "").strip()
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "").strip()
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "").strip()
    oidc_allowed_domains: str = os.getenv("OIDC_ALLOWED_DOMAINS", "").strip()
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
    ai_seller_enabled: bool = os.getenv("AI_SELLER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    ai_supervisor_shadow_enabled: bool = os.getenv(
        "AI_SUPERVISOR_SHADOW_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_supervisor_model: str = os.getenv("AI_SUPERVISOR_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
    ai_supervisor_reasoning_effort: str = (
        os.getenv("AI_SUPERVISOR_REASONING_EFFORT", "low").strip() or "low"
    )
    ai_supervisor_poll_seconds: int = int(os.getenv("AI_SUPERVISOR_POLL_SECONDS", "4"))
    ai_supervisor_guardian_enabled: bool = os.getenv(
        "AI_SUPERVISOR_GUARDIAN_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_supervisor_guardian_timeout_seconds: int = int(
        os.getenv("AI_SUPERVISOR_GUARDIAN_TIMEOUT_SECONDS", "30")
    )
    ai_autocorrect_enabled: bool = os.getenv(
        "AI_AUTOCORRECT_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_autocorrect_poll_seconds: int = int(os.getenv("AI_AUTOCORRECT_POLL_SECONDS", "30"))
    ai_learning_enabled: bool = os.getenv(
        "AI_LEARNING_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_learning_poll_seconds: int = int(os.getenv("AI_LEARNING_POLL_SECONDS", "300"))
    ai_learning_min_samples_per_arm: int = int(
        os.getenv("AI_LEARNING_MIN_SAMPLES_PER_ARM", "30")
    )
    ai_learning_min_uplift: float = float(
        os.getenv("AI_LEARNING_MIN_UPLIFT", "0.05")
    )
    ai_engineer_enabled: bool = os.getenv(
        "AI_ENGINEER_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_engineer_poll_seconds: int = int(os.getenv("AI_ENGINEER_POLL_SECONDS", "300"))
    ai_engineer_min_incidents: int = int(os.getenv("AI_ENGINEER_MIN_INCIDENTS", "3"))
    ai_engineer_autonomous_pr_enabled: bool = os.getenv(
        "AI_ENGINEER_AUTONOMOUS_PR_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    ai_engineer_github_repository: str = os.getenv(
        "AI_ENGINEER_GITHUB_REPOSITORY",
        "rafaelsantos-coder/Sistema-Integrado",
    ).strip()
    ai_engineer_github_token: str = os.getenv("AI_ENGINEER_GITHUB_TOKEN", "").strip()
    sulnet_crm_base_url: str = os.getenv("SULNET_CRM_BASE_URL", "").rstrip("/")
    sulnet_crm_bearer_token: str = os.getenv("SULNET_CRM_BEARER_TOKEN", "")
    sulnet_ai_integration_key: str = os.getenv("SULNET_AI_INTEGRATION_KEY", "")
    media_s3_endpoint: str = os.getenv("MEDIA_S3_ENDPOINT", "")
    media_s3_bucket: str = os.getenv("MEDIA_S3_BUCKET", "")
    media_s3_region: str = os.getenv("MEDIA_S3_REGION", "auto")
    media_s3_access_key_id: str = os.getenv("MEDIA_S3_ACCESS_KEY_ID", "")
    media_s3_secret_access_key: str = os.getenv("MEDIA_S3_SECRET_ACCESS_KEY", "")
    media_s3_url_style: str = os.getenv("MEDIA_S3_URL_STYLE", "virtual-host")
    media_max_upload_mb: int = int(os.getenv("MEDIA_MAX_UPLOAD_MB", "50"))
    backup_encryption_key: str = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    backup_retention_days: int = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
    media_remote_allowed_hosts: str = os.getenv("MEDIA_REMOTE_ALLOWED_HOSTS", "").strip()
    clamav_host: str = os.getenv("CLAMAV_HOST", "").strip()
    clamav_port: int = int(os.getenv("CLAMAV_PORT", "3310"))
    clamav_required: bool = os.getenv(
        "CLAMAV_REQUIRED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    trusted_hosts: str = os.getenv("TRUSTED_HOSTS", "").strip()
    whatsapp_presence_timeout_seconds: int = int(os.getenv("WHATSAPP_PRESENCE_TIMEOUT_SECONDS", "90"))
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def oidc_configured(self) -> bool:
        return bool(
            self.oidc_metadata_url
            and self.oidc_client_id
            and self.oidc_client_secret
        )

    @property
    def required_mfa_roles(self) -> set[str]:
        return {
            item.strip().upper()
            for item in self.mfa_required_roles.split(",")
            if item.strip()
        }

    @property
    def effective_session_signing_key(self) -> str:
        return self.session_signing_key or self.secret_key

    @property
    def effective_data_encryption_key(self) -> str:
        return self.data_encryption_key or self.secret_key

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
            "ai_seller": bool(
                self.ai_seller_enabled
                and self.openai_api_key
                and self.sulnet_crm_base_url
                and (self.sulnet_ai_integration_key or self.sulnet_crm_bearer_token)
            ),
            "ai_supervisor_shadow": bool(
                self.ai_supervisor_shadow_enabled
                and self.openai_api_key
            ),
            "ai_supervisor_guardian": bool(
                self.ai_supervisor_guardian_enabled
                and self.openai_api_key
            ),
            "ai_autocorrect": bool(self.ai_autocorrect_enabled),
            "ai_learning": bool(self.ai_learning_enabled and self.openai_api_key),
            "ai_engineer": bool(self.ai_engineer_enabled and self.openai_api_key),
            "redis": bool(self.redis_url),
            "clamav": bool(self.clamav_host),
            "oidc_sso": self.oidc_configured,
            "ai_engineer_autonomous_pr": bool(
                self.ai_engineer_enabled
                and self.ai_engineer_autonomous_pr_enabled
                and self.openai_api_key
                and self.ai_engineer_github_repository
                and self.ai_engineer_github_token
            ),
        }


settings = Settings()


def validate_production_settings() -> None:
    if not settings.is_production:
        return
    errors: list[str] = []
    if settings.secret_key == "dev-only-change-this-secret" or len(settings.secret_key) < 32:
        errors.append("SECRET_KEY deve ter ao menos 32 caracteres e não pode usar o valor padrão")
    if len(settings.effective_session_signing_key) < 32:
        errors.append("SESSION_SIGNING_KEY/SECRET_KEY deve ter ao menos 32 caracteres")
    if len(settings.effective_data_encryption_key) < 32:
        errors.append("DATA_ENCRYPTION_KEY/SECRET_KEY deve ter ao menos 32 caracteres")
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
    if settings.ai_seller_enabled:
        if not settings.sulnet_crm_base_url.startswith(("https://", "http://")):
            errors.append("AI_SELLER_ENABLED requer SULNET_CRM_BASE_URL válida")
        if (
            len(settings.sulnet_ai_integration_key) < 24
            and len(settings.sulnet_crm_bearer_token) < 24
        ):
            errors.append(
                "AI_SELLER_ENABLED requer SULNET_AI_INTEGRATION_KEY ou "
                "SULNET_CRM_BEARER_TOKEN com ao menos 24 caracteres"
            )
    if errors:
        raise RuntimeError("Configuração de produção inválida: " + "; ".join(errors))
