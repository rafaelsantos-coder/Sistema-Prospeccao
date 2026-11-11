from __future__ import annotations

import fnmatch
import ipaddress
from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from app.config import settings


def trusted_hosts() -> list[str]:
    configured = [
        item.strip()
        for item in settings.trusted_hosts.split(",")
        if item.strip()
    ]
    if configured:
        return configured

    hosts = [
        "localhost",
        "127.0.0.1",
        "100.64.0.1",
        "*.railway.internal",
    ]
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
        if request.url.path == "/reports/credit-map":
            # Exceção restrita a esta página; as demais continuam sem scripts externos.
            policy = response.headers["Content-Security-Policy"]
            response.headers["Content-Security-Policy"] = policy.replace(
                "script-src 'self' 'unsafe-inline'",
                "script-src 'self' 'unsafe-inline' https://maps.googleapis.com https://maps.gstatic.com",
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


def _host_allowed(host: str) -> bool:
    clean = (host or "").split(":", 1)[0].strip().casefold()
    if not clean:
        return False
    for pattern in trusted_hosts():
        item = pattern.casefold()
        if item.startswith("*."):
            if fnmatch.fnmatch(clean, item):
                return True
        elif clean == item:
            return True
    return False


def _is_internal_healthcheck(request: Request) -> bool:
    if request.url.path != "/api/health":
        return False
    client_host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip in cgnat
    )


class SulnetTrustedHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if _is_internal_healthcheck(request):
            return await call_next(request)

        host = request.headers.get("host", "")
        if not _host_allowed(host):
            return PlainTextResponse(
                "Invalid host header",
                status_code=400,
            )
        return await call_next(request)


def install_security_middlewares(app) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.is_production:
        app.add_middleware(SulnetTrustedHostMiddleware)
