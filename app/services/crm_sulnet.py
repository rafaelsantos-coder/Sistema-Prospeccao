from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class CRMIntegrationError(RuntimeError):
    pass


@dataclass(slots=True)
class SulnetCRMClient:
    base_url: str
    bearer_token: str
    timeout: float = 25.0

    @classmethod
    def from_settings(cls) -> "SulnetCRMClient":
        if not settings.sulnet_crm_base_url or not settings.sulnet_crm_bearer_token:
            raise CRMIntegrationError("Integração com o CRM Sulnet não configurada.")
        return cls(
            base_url=settings.sulnet_crm_base_url.rstrip("/"),
            bearer_token=settings.sulnet_crm_bearer_token,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            with httpx.Client(
                timeout=timeout or self.timeout,
                follow_redirects=False,
                headers={
                    "Authorization": f"Bearer {self.bearer_token}",
                    "Accept": "application/json",
                    "User-Agent": "Sulnet-Prospecta-Vendedor-IA/1.0",
                },
            ) as client:
                response = client.request(
                    method.upper(),
                    url,
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise CRMIntegrationError("CRM Sulnet indisponível no momento.") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CRMIntegrationError(
                f"CRM Sulnet retornou resposta inválida (HTTP {response.status_code})."
            ) from exc

        if response.status_code >= 400:
            message = (
                payload.get("error")
                if isinstance(payload, dict)
                else None
            ) or f"Falha no CRM Sulnet (HTTP {response.status_code})."
            raise CRMIntegrationError(str(message)[:600])
        return payload

    def config(self) -> dict[str, Any]:
        result = self._request("GET", "/api/capture/config")
        return result if isinstance(result, dict) else {}

    def create_opportunity(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/api/capture/leads", json_body=payload)
        return result if isinstance(result, dict) else {}

    def update_opportunity(self, opportunity_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            f"/api/capture/leads/{opportunity_id}",
            json_body=payload,
        )
        return result if isinstance(result, dict) else {}

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/capture/leads/{opportunity_id}")
        return result if isinstance(result, dict) else {}

    def consult_credit(self, opportunity_id: int, cpf: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/api/capture/leads/{opportunity_id}/consult",
            json_body={"cpf": cpf},
            timeout=45.0,
        )
        return result if isinstance(result, dict) else {}

    def lookup_cep(self, cep: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/capture/cep/{cep}")
        return result if isinstance(result, dict) else {}

    def products(self, query: str = "") -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/api/capture/products",
            params={"q": query} if query else None,
        )
        return result if isinstance(result, list) else []

    def mark_won(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/api/capture/leads/{opportunity_id}/win",
            json_body={},
        )
        return result if isinstance(result, dict) else {}

    def get_status(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/capture/leads/{opportunity_id}/status")
        return result if isinstance(result, dict) else {}

    def get_contract(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/capture/leads/{opportunity_id}/contract")
        return result if isinstance(result, dict) else {}

    def start_contract(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/api/capture/leads/{opportunity_id}/contract",
            json_body={},
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}
