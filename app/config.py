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
    evolution_base_url: str = os.getenv("EVOLUTION_BASE_URL", "")
    evolution_api_key: str = os.getenv("EVOLUTION_API_KEY", "")
    evolution_instance: str = os.getenv("EVOLUTION_INSTANCE", "")
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    resend_from_email: str = os.getenv("RESEND_FROM_EMAIL", "")
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def integrations(self) -> dict[str, bool]:
        return {
            "apify": bool(self.apify_token),
            "whatsapp": bool(
                self.evolution_base_url and self.evolution_api_key and self.evolution_instance
            ),
            "email": bool(self.resend_api_key and self.resend_from_email),
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
    if errors:
        raise RuntimeError("Configuração de produção inválida: " + "; ".join(errors))
