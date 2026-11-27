"""Adaptador de homologacao OpenWA - somente leitura, isolado e opt-in.

Nao importa o codigo de terceiros, nao abre webhooks e nao envia mensagens.
Credenciais e destino sao definidos somente por variaveis de ambiente.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

import httpx


class OpenWAError(RuntimeError):
    """Erro sanitizado, seguro para mostrar na interface administrativa."""


_SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
)
_STATUSES = frozenset({
    "created", "initializing", "qr_ready", "authenticating",
    "ready", "disconnected", "action_required", "failed",
})
_MAX_STATUS_BYTES = 16 * 1024


@dataclass(frozen=True)
class OpenWAConfig:
    enabled: bool = False
    base_url: str = ""
    allowed_host: str = ""
    api_key: str = ""
    session_id: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OpenWAConfig":
        env = os.environ if env is None else env
        return cls(
            enabled=env.get("OPENWA_ENABLED", "").strip().lower()
            in {"1", "true", "yes", "on"},
            base_url=env.get("OPENWA_BASE_URL", "").strip(),
            allowed_host=env.get("OPENWA_ALLOWED_HOST", "").strip().lower(),
            api_key=env.get("OPENWA_API_KEY", "").strip(),
            session_id=env.get("OPENWA_SESSION_ID", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """Apenas presenca, nao valida a rede nem efetua consultas."""
        return bool(self.base_url and self.allowed_host and self.api_key and self.session_id)

    def endpoint(self) -> str:
        if not self.enabled:
            raise OpenWAError("OpenWA desativado para homologacao.")
        if not self.is_configured:
            raise OpenWAError("Configure as quatro variaveis OPENWA antes de testar.")
        if not _SESSION_RE.fullmatch(self.session_id):
            raise OpenWAError("Identificador da sessao OpenWA invalido.")
        try:
            uri = urlsplit(self.base_url)
            host = (uri.hostname or "").lower()
            port = uri.port
        except ValueError as exc:
            raise OpenWAError("Destino OpenWA invalido.") from exc
        if (
            not self.allowed_host
            or not _HOST_RE.fullmatch(self.allowed_host)
            or not _HOST_RE.fullmatch(host)
            or host != self.allowed_host
            or uri.username is not None
            or uri.password is not None
            or uri.path not in {"", "/"}
            or uri.query
            or uri.fragment
        ):
            raise OpenWAError("Destino OpenWA nao autorizado.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise OpenWAError("IPs diretos nao sao autorizados para OpenWA.")
        if host == "localhost" or host.endswith(".localhost"):
            raise OpenWAError("Host local nao autorizado para OpenWA.")
        internal = host.endswith(".railway.internal")
        if uri.scheme != "https" and not (uri.scheme == "http" and internal):
            raise OpenWAError("Use HTTPS ou rede privada do Railway.")
        if port is not None and (port < 1 or port > 65535):
            raise OpenWAError("Porta OpenWA invalida.")
        return f"{self.base_url.rstrip('/')}/api/sessions/{self.session_id}"


class OpenWAClient:
    """Consulta exclusivamente o status da sessao, sem envio e sem webhooks."""

    def __init__(
        self,
        config: OpenWAConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    def status(self) -> dict[str, object]:
        endpoint = self.config.endpoint()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(5.0, connect=3.0),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "GET",
                    endpoint,
                    headers={
                        "X-API-Key": self.config.api_key,
                        "Accept": "application/json",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise OpenWAError("OpenWA nao confirmou a consulta de status.")
                    if int(response.headers.get("content-length", "0") or "0") > _MAX_STATUS_BYTES:
                        raise OpenWAError("Resposta OpenWA excedeu o tamanho permitido.")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_STATUS_BYTES:
                            raise OpenWAError("Resposta OpenWA excedeu o tamanho permitido.")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenWAError("Falha segura ao consultar o OpenWA.") from exc
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenWAError("Resposta OpenWA invalida.") from exc
        if not isinstance(payload, dict) or payload.get("id") != self.config.session_id:
            raise OpenWAError("Sessao OpenWA nao corresponde a configuracao.")
        state = payload.get("status")
        return {
            "session": self.config.session_id,
            "status": state if isinstance(state, str) and state in _STATUSES else "desconhecido",
            "engine_loaded": payload.get("engineLoaded") is True,
        }
