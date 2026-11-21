from __future__ import annotations

import fnmatch
import ipaddress
import secrets
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
        # Um nonce por resposta: só scripts da própria página (com o nonce) rodam; script injetado não.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy",
            "strict-origin-when-cross-origin",
        )
        # microphone=(self): o chat das Conversas usa getUserMedia no próprio domínio.
        # microphone=() bloqueava a captura e o Chrome caía em InvalidStateError
        # no fluxo nativo ("Usar o microfone").
        # Nao usar setdefault: a politica de uma resposta downstream nao pode
        # desativar a captura de audio das Conversas novamente.
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(self), geolocation=(), payment=()"
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
                    f"script-src 'self' 'nonce-{nonce}'",
                    "style-src 'self' 'unsafe-inline'",
                    "img-src 'self' data: blob: https:",
                    "media-src 'self' blob: https:",
                    "font-src 'self' data:",
                    "connect-src 'self' https:",
                ]
            ),
        )
        map_path = request.url.path
        if map_path in {"/reports/credit-map", "/feasibility-map"}:
            # Leaflet e Google Maps ficam restritos às páginas que usam essas bibliotecas.
            policy = response.headers["Content-Security-Policy"]
            external = "https://unpkg.com https://cdnjs.cloudflare.com"
            policy = policy.replace(
                f"script-src 'self' 'nonce-{nonce}'", f"script-src 'self' 'nonce-{nonce}' {external}"
            ).replace("style-src 'self' 'unsafe-inline'", f"style-src 'self' 'unsafe-inline' {external}")
            if settings.google_maps_browser_key and (
                map_path == "/feasibility-map" or settings.credit_map_provider == "google"
            ):
                # A API dinamica do Google herda o nonce definido no carregador da pagina.
                policy = policy.replace(
                    f"script-src 'self' 'nonce-{nonce}' {external}",
                    f"script-src 'self' 'nonce-{nonce}' {external} "
                    "https://maps.googleapis.com https://maps.gstatic.com "
                    "https://mapsresources-pa.googleapis.com 'unsafe-eval' blob:",
                )
                policy += "; frame-src https://*.google.com; worker-src blob:"
                if map_path == "/feasibility-map":
                    policy = policy.replace("font-src 'self'", "font-src 'self' https://fonts.gstatic.com")
                    policy = policy.replace(f"style-src 'self' 'unsafe-inline' {external}",
                                            f"style-src 'self' 'unsafe-inline' {external} https://fonts.googleapis.com")
            response.headers["Content-Security-Policy"] = policy
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )

        if request.url.path.startswith(
            ("/login", "/change-password", "/settings", "/conversations", "/feasibility-map")
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
