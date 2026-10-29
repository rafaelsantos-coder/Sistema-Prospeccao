from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import unicodedata
from typing import Any

import httpx

from app.config import settings


LOG = logging.getLogger("sulnet_crm_client")


class CRMIntegrationError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
                f"CRM Sulnet retornou resposta inválida (HTTP {response.status_code}).",
                status_code=response.status_code,
            ) from exc

        if response.status_code >= 400:
            message = (
                payload.get("error")
                if isinstance(payload, dict)
                else None
            ) or f"Falha no CRM Sulnet (HTTP {response.status_code})."
            raise CRMIntegrationError(
                str(message)[:600],
                status_code=response.status_code,
            )
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

    def _data_stage_id(self) -> int | None:
        try:
            context = self.context()
        except CRMIntegrationError:
            return None
        stages = context.get("stages")
        if not isinstance(stages, list):
            return None
        for item in stages:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if "dados e plano" in name:
                try:
                    return int(item.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    def _fallback_update_opportunity(
        self,
        opportunity_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current: dict[str, Any] = {}
        try:
            current = self.get_opportunity(opportunity_id)
        except CRMIntegrationError:
            current = {}

        generic: dict[str, Any] = {}
        direct_keys = {
            "clientName",
            "clientPhone",
            "clientEmail",
            "cep",
            "city",
            "uf",
            "state",
            "street",
            "number",
            "addressNumber",
            "neighborhood",
            "complement",
        }
        for key in direct_keys:
            if key in payload:
                generic[key] = payload[key]

        if payload.get("cpf") not in (None, ""):
            generic["clientCpf"] = payload["cpf"]
        if payload.get("clientCpf") not in (None, ""):
            generic["clientCpf"] = payload["clientCpf"]

        ai_closing = current.get("aiClosing")
        if not isinstance(ai_closing, dict):
            ai_closing = {}
        else:
            ai_closing = dict(ai_closing)

        for key in ("planLabel", "paymentMethod", "dueDay", "invoiceDelivery"):
            if payload.get(key) not in (None, ""):
                ai_closing[key] = payload[key]

        selected_product: dict[str, Any] | None = None
        plan_id = payload.get("planId")
        if plan_id not in (None, ""):
            try:
                target_id = int(plan_id)
            except (TypeError, ValueError):
                target_id = 0
            if target_id:
                products = self.products("")
                selected_product = next(
                    (
                        item
                        for item in products
                        if int(item.get("id") or 0) == target_id
                    ),
                    None,
                )
                generic["planIds"] = [target_id]

        plan_label = str(payload.get("planLabel") or "").strip()
        if not selected_product and plan_label:
            products = self.products(plan_label)

            exact = [
                item
                for item in products
                if self._fold_catalog_text(str(item.get("name") or ""))
                == self._fold_catalog_text(plan_label)
            ]
            if len(exact) == 1:
                selected_product = exact[0]
            elif len(products) == 1:
                selected_product = products[0]
            else:
                expected_value = payload.get("monthlyValue")
                try:
                    expected_value_float = (
                        round(float(expected_value), 2)
                        if expected_value not in (None, "")
                        else None
                    )
                except (TypeError, ValueError):
                    expected_value_float = None

                if expected_value_float is not None:
                    same_price = []
                    for item in products:
                        try:
                            catalog_value = round(float(item.get("monthlyValue")), 2)
                        except (TypeError, ValueError):
                            continue
                        if abs(catalog_value - expected_value_float) < 0.01:
                            same_price.append(item)
                    if len(same_price) == 1:
                        selected_product = same_price[0]

            if not selected_product and expected_value_float is not None:
                # O nome comercial usado no WhatsApp pode ser diferente do nome
                # cadastrado no ERP. Ex.: "Sulmóvel 15GB" pode estar no catálogo
                # como outro nome comercial, mas com o mesmo pacote de dados.
                all_products = self.products("")
                folded_label = self._fold_catalog_text(plan_label).replace(" ", "")
                capacity_match = re.search(r"(\d{1,3}gb)", folded_label)
                capacity = capacity_match.group(1) if capacity_match else ""

                pool = all_products
                if capacity:
                    same_capacity = [
                        item
                        for item in all_products
                        if capacity
                        in self._fold_catalog_text(str(item.get("name") or "")).replace(" ", "")
                    ]
                    if same_capacity:
                        pool = same_capacity

                # Se o rótulo indica móvel, privilegia a categoria móvel do catálogo,
                # sem depender de um ID fixo (ele varia entre ambientes).
                if "movel" in folded_label or "sulmovel" in folded_label:
                    mobile_pool = [
                        item
                        for item in pool
                        if "movel"
                        in self._fold_catalog_text(
                            str(item.get("categoryName") or item.get("name") or "")
                        )
                    ]
                    if mobile_pool:
                        pool = mobile_pool

                same_price = []
                for item in pool:
                    try:
                        catalog_value = round(float(item.get("monthlyValue")), 2)
                    except (TypeError, ValueError):
                        continue
                    if abs(catalog_value - expected_value_float) < 0.01:
                        same_price.append(item)
                if len(same_price) == 1:
                    selected_product = same_price[0]

            if selected_product:
                generic["planIds"] = [int(selected_product.get("id") or 0)]
            elif "planIds" not in generic:
                LOG.warning(
                    "crm_plan_ambiguous label=%s candidates=%s expected_value=%s",
                    plan_label[:120],
                    len(products),
                    payload.get("monthlyValue"),
                )
                raise CRMIntegrationError(
                    "Plano não localizado de forma inequívoca no catálogo do CRM.",
                    status_code=409,
                )

        if selected_product:
            if selected_product.get("categoryId") not in (None, ""):
                generic["categoryId"] = selected_product.get("categoryId")
            if selected_product.get("monthlyValue") not in (None, ""):
                generic["monthlyValue"] = selected_product.get("monthlyValue")
            if not plan_label:
                ai_closing["planLabel"] = str(selected_product.get("name") or "")

        if ai_closing:
            generic["productInfo"] = {"aiClosing": ai_closing}

        if any(
            key in payload
            for key in ("planId", "planLabel", "paymentMethod", "dueDay", "invoiceDelivery")
        ):
            stage_id = self._data_stage_id()
            if stage_id:
                generic["stageId"] = stage_id

        if not generic:
            return {}

        LOG.warning(
            "crm_patch_fallback opportunity_id=%s keys=%s",
            opportunity_id,
            sorted(generic.keys()),
        )
        result = self._request(
            "PATCH",
            f"/api/opportunities/{opportunity_id}",
            json_body=generic,
        )
        return result if isinstance(result, dict) else {}

    def update_opportunity(self, opportunity_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._request(
                "PATCH",
                f"/api/capture/leads/{opportunity_id}",
                json_body=payload,
            )
            return result if isinstance(result, dict) else {}
        except CRMIntegrationError as exc:
            if exc.status_code != 404:
                raise
            return self._fallback_update_opportunity(opportunity_id, payload)

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/capture/leads/{opportunity_id}")
        return result if isinstance(result, dict) else {}

    def update_category_form(
        self,
        opportunity_id: int,
        values: dict[str, Any],
        *,
        category_id: int | None = None,
        plan_id: int | None = None,
    ) -> dict[str, Any]:
        current = self.get_opportunity(opportunity_id)
        ai_closing = current.get("aiClosing")
        if not isinstance(ai_closing, dict):
            ai_closing = {}

        payload: dict[str, Any] = {
            "productInfo": {
                "aiClosing": dict(ai_closing),
                "categoryForm": {
                    "values": dict(values),
                    # No funil SDR IA, documentos/selfie não são coletados no chat.
                    "files": {},
                },
            }
        }
        if category_id:
            payload["categoryId"] = int(category_id)
        if plan_id:
            payload["planIds"] = [int(plan_id)]

        result = self._request(
            "PATCH",
            f"/api/opportunities/{opportunity_id}",
            json_body=payload,
        )
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

    @staticmethod
    def _fold_catalog_text(value: str) -> str:
        text = unicodedata.normalize("NFD", str(value or "").casefold())
        return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

    def _catalog_products_fallback(self, query: str = "") -> list[dict[str, Any]]:
        result = self._request("GET", "/api/catalog/products")
        if not isinstance(result, list):
            return []

        products: list[dict[str, Any]] = []
        for row in result:
            if not isinstance(row, dict):
                continue

            # Algumas versões do CRM retornam catálogo plano.
            if row.get("id") not in (None, "") and row.get("name"):
                products.append(
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "monthlyValue": row.get("monthlyValue", row.get("monthly_value")),
                        "categoryId": row.get("categoryId", row.get("category_id")),
                        "categoryName": row.get("categoryName", row.get("category_name")),
                    }
                )
                continue

            # Versão atual agrupa por categoria e serializa products.
            raw_products = row.get("products")
            if isinstance(raw_products, str):
                try:
                    raw_products = json.loads(raw_products)
                except json.JSONDecodeError:
                    raw_products = []
            if not isinstance(raw_products, list):
                continue

            category_id = row.get("category_id", row.get("categoryId"))
            category_name = row.get("category_name", row.get("categoryName"))
            for product in raw_products:
                if not isinstance(product, dict):
                    continue
                products.append(
                    {
                        "id": product.get("id"),
                        "name": product.get("name"),
                        "monthlyValue": product.get(
                            "monthlyValue",
                            product.get("monthly_value"),
                        ),
                        "categoryId": product.get(
                            "categoryId",
                            product.get("category_id", category_id),
                        ),
                        "categoryName": product.get(
                            "categoryName",
                            product.get("category_name", category_name),
                        ),
                    }
                )

        if not query:
            return products

        tokens = [
            token
            for token in self._fold_catalog_text(query).split()
            if len(token) >= 2
        ]
        if not tokens:
            return products
        return [
            item
            for item in products
            if all(
                token in self._fold_catalog_text(str(item.get("name") or ""))
                for token in tokens
            )
        ]

    def products(self, query: str = "") -> list[dict[str, Any]]:
        try:
            result = self._request(
                "GET",
                "/api/capture/products",
                params={"q": query} if query else None,
            )
            return result if isinstance(result, list) else []
        except CRMIntegrationError as exc:
            if exc.status_code != 404:
                raise
            LOG.warning("crm_products_fallback source=catalog query_present=%s", bool(query))
            return self._catalog_products_fallback(query)

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
