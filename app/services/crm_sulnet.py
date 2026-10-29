from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import httpx

from app.config import settings


LOG = logging.getLogger("sulnet_crm_client")


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

        LOG.info(
            "crm_http method=%s path=%s status=%s",
            method.upper(),
            path,
            response.status_code,
        )

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

    def context(self) -> dict[str, Any]:
        result = self._request("GET", "/api/capture/context")
        return result if isinstance(result, dict) else {}

    def config(self) -> dict[str, Any]:
        # Compatibilidade interna: o CRM on-prem expõe o contexto operacional
        # (origem/funil/permissões) em /api/capture/context.
        return self.context()

    def resolve_origin_id(self) -> int | None:
        context = self.context()
        direct = context.get("originId")
        if direct not in (None, ""):
            try:
                return int(direct)
            except (TypeError, ValueError):
                pass
        origin = context.get("origin")
        if isinstance(origin, dict) and origin.get("id") not in (None, ""):
            try:
                return int(origin["id"])
            except (TypeError, ValueError):
                pass
        origins = context.get("origins")
        if isinstance(origins, list):
            for item in origins:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("label") or "").strip().lower()
                if "sdr ia" in name or "whatsapp" in name:
                    try:
                        return int(item.get("id"))
                    except (TypeError, ValueError):
                        continue
        return None

    def create_opportunity(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload = dict(payload)
        request_payload.setdefault("runConsult", False)
        if request_payload.get("originId") in (None, ""):
            origin_id = self.resolve_origin_id()
            if origin_id is not None:
                request_payload["originId"] = origin_id

        result = self._request("POST", "/api/capture/leads", json_body=request_payload)
        if not isinstance(result, dict):
            return {}

        # O CRM on-prem atual devolve os dados em result.lead. Normalizamos para
        # o contrato interno do Prospecta sem perder os campos originais.
        lead = result.get("lead")
        if isinstance(lead, dict):
            normalized = dict(result)
            normalized["opportunityId"] = lead.get("id")
            normalized["protocol"] = lead.get("protocol")
            normalized["status"] = lead.get("status", result.get("status"))
            normalized["created"] = result.get("created", True)
            LOG.info(
                "crm_lead_result opportunity_id=%s protocol=%s created=%s",
                normalized.get("opportunityId"),
                normalized.get("protocol") or "-",
                normalized.get("created"),
            )
            return normalized
        return result

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

    def consult_credit(
        self,
        opportunity_id: int,
        cpf: str | None = None,
        *,
        birth_date: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if cpf:
            body["clientCpf"] = cpf
        if birth_date:
            body["clientBirthDate"] = birth_date
        if extra_fields:
            for key in ("cep", "street", "addressNumber", "neighborhood", "uf", "city"):
                value = extra_fields.get(key)
                if value not in (None, ""):
                    body[key] = value

        result = self._request(
            "POST",
            f"/api/capture/leads/{opportunity_id}/consult",
            json_body=body,
            timeout=45.0,
        )
        if not isinstance(result, dict):
            return {}

        analysis = result.get("analysis")
        if not isinstance(analysis, dict):
            analysis = {}
        normalized = dict(result)
        status = str(
            result.get("status")
            or analysis.get("status")
            or (result.get("lead") or {}).get("creditStatus")
            or ""
        ).strip()
        normalized.setdefault("status", status)
        normalized.setdefault("analysisId", analysis.get("id") or result.get("analysisId"))

        if "approved" not in normalized:
            if "approved" in analysis:
                normalized["approved"] = bool(analysis.get("approved"))
            else:
                key = status.lower()
                normalized["approved"] = any(
                    token in key for token in ("aprovado", "liberado", "pre-aprovado")
                )
        if "rejected" not in normalized:
            if "rejected" in analysis:
                normalized["rejected"] = bool(analysis.get("rejected"))
            else:
                normalized["rejected"] = "reprov" in status.lower()
        if "manualReview" not in normalized:
            normalized["manualReview"] = not normalized["approved"] and not normalized["rejected"]
        if "canContest" not in normalized:
            normalized["canContest"] = (
                bool(analysis.get("canContest"))
                if "canContest" in analysis
                else bool(normalized["rejected"])
            )

        LOG.info(
            "crm_credit_result opportunity_id=%s analysis_id=%s status=%s approved=%s rejected=%s",
            opportunity_id,
            normalized.get("analysisId"),
            status or "-",
            bool(normalized.get("approved")),
            bool(normalized.get("rejected")),
        )
        return normalized

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
