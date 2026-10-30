from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings


def trusted_hosts() -> list[str]:
    configured = [
        item.strip()
        for item in settings.trusted_hosts.split(",")
        if item.strip()
    ]
    if configured:
        return configured

    hosts = ["localhost", "127.0.0.1", "*.railway.internal"]
    if settings.app_public_url:
        host = urlparse(settings.app_public_url).hostname
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy",
            "strict-origin-when-cross-origin",
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                [
                    "default-src 'self'",
                    "base-uri 'self'",
                    "frame-ancestors 'none'",
                    "form-action 'self'",
                    "object-src 'none'",
                    "script-src 'self' 'unsafe-inline'",
                    "style-src 'self' 'unsafe-inline'",
                    "img-src 'self' data: blob: https:",
                    "media-src 'self' blob: https:",
                    "font-src 'self' data:",
                    "connect-src 'self' https:",
                ]
            ),
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )

        if request.url.path.startswith(
            ("/login", "/change-password", "/settings", "/conversations")
        ):
            response.headers.setdefault("Cache-Control", "no-store")

        return response


def install_security_middlewares(app) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=trusted_hosts(),
            www_redirect=False,
        )
