from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings


class RemoteURLValidationError(ValueError):
    pass


def _host_matches_allowlist(host: str) -> bool:
    configured = [
        item.strip().casefold()
        for item in settings.media_remote_allowed_hosts.split(",")
        if item.strip()
    ]
    if not configured:
        return True
    host = host.casefold().rstrip(".")
    for item in configured:
        item = item.rstrip(".")
        if item.startswith("*."):
            suffix = item[1:]
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == item:
            return True
    return False


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_remote_url(value: str) -> str:
    parsed = urlparse(value)
    allowed_schemes = {"https"} if settings.is_production else {"http", "https"}
    if parsed.scheme.casefold() not in allowed_schemes:
        raise RemoteURLValidationError("URL remota usa protocolo não permitido.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise RemoteURLValidationError("URL remota inválida.")

    host = parsed.hostname.rstrip(".")
    if not _host_matches_allowlist(host):
        raise RemoteURLValidationError("Host remoto não autorizado.")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise RemoteURLValidationError("Host remoto não pôde ser resolvido.") from exc

    if not addresses or not all(_public_ip(address) for address in addresses):
        raise RemoteURLValidationError("URL remota aponta para rede não pública.")

    return value
