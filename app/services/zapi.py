from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import settings


class ZApiError(RuntimeError):
    """Erro seguro da integracao Z-API, sem expor credenciais em logs."""


class ZApiNotConfigured(ZApiError):
    pass


class ZApiService:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        instance_id: str | None = None,
        instance_token: str | None = None,
        client_token: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.zapi_base_url).rstrip("/")
        self.instance_id = instance_id or settings.zapi_instance_id
        self.instance_token = instance_token or settings.zapi_instance_token
        self.client_token = client_token if client_token is not None else settings.zapi_client_token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.instance_id and self.instance_token)

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ZApiNotConfigured("A integracao com a Z-API ainda nao foi configurada.")

    def _headers(self, *, json_request: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if json_request:
            headers["Content-Type"] = "application/json"
        if self.client_token:
            headers["Client-Token"] = self.client_token
        return headers

    def _url(self, endpoint: str) -> str:
        self._ensure_configured()
        return (
            f"{self.base_url}/instances/{self.instance_id}"
            f"/token/{self.instance_token}/{endpoint.lstrip('/')}"
        )

    @staticmethod
    def normalize_phone(phone: str | None) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) in {10, 11}:
            digits = "55" + digits
        if not 12 <= len(digits) <= 15:
            raise ZApiError("Informe um telefone valido com DDI e DDD.")
        return digits

    @staticmethod
    def _safe_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message")
            if message:
                return str(message)[:240]
        return f"HTTP {response.status_code}"

    def status(self) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self._url("status"), headers=self._headers())
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel consultar o status da Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha ao consultar Z-API: {self._safe_error(response)}")
        payload = response.json()
        return {
            "configured": True,
            "connected": bool(payload.get("connected")),
            "smartphone_connected": bool(payload.get("smartphoneConnected")),
            "error": payload.get("error"),
        }

    def send_text(self, *, phone: str, message: str) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        clean_message = message.strip()
        if not clean_message:
            raise ZApiError("A mensagem nao pode ficar vazia.")
        if len(clean_message) > 4096:
            raise ZApiError("A mensagem deve ter no maximo 4.096 caracteres.")
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self._url("send-text"),
                    headers=self._headers(json_request=True),
                    json={"phone": normalized_phone, "message": clean_message},
                )
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel enviar a mensagem pela Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha no envio pela Z-API: {self._safe_error(response)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ZApiError("A Z-API retornou uma resposta inesperada.")
        return payload

    def configure_webhooks(self, webhook_url: str) -> dict[str, Any]:
        clean_url = webhook_url.strip()
        if not clean_url.startswith("https://"):
            raise ZApiError("O webhook da Z-API precisa usar HTTPS.")
        results: dict[str, Any] = {}
        endpoints = {
            "received": "update-webhook-received",
            "message_status": "update-webhook-message-status",
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                for key, endpoint in endpoints.items():
                    response = client.put(
                        self._url(endpoint),
                        headers=self._headers(json_request=True),
                        json={"value": clean_url},
                    )
                    if response.status_code >= 400:
                        raise ZApiError(
                            f"Falha ao configurar webhook {key}: {self._safe_error(response)}"
                        )
                    try:
                        results[key] = response.json()
                    except ValueError:
                        results[key] = {"ok": True}
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel configurar os webhooks da Z-API.") from exc
        return results
