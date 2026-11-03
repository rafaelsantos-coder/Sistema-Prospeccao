from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import unicodedata
from typing import Any, Literal
import uuid

import httpx

from app.config import settings


LOG = logging.getLogger("sulnet_crm_client")


class CRMIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str = "",
        path: str = "",
        error_code: str = "",
        response_body: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.path = path
        self.error_code = error_code
        self.response_body = response_body

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "statusCode": self.status_code,
            "method": self.method,
            "path": self.path,
            "errorCode": self.error_code,
            "responseBody": self.response_body,
        }


@dataclass(slots=True)
class CRMPlaybookMissingItem:
    code: str
    label: str = ""
    question: str = ""
    group: str = ""
    patch_key: str = ""
    suggested_value: Any = None
    options: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> "CRMPlaybookMissingItem | None":
        if not isinstance(payload, dict):
            return None
        code = str(payload.get("code") or "").strip()
        if not code:
            return None
        raw_options = (
            payload.get("options")
            or payload.get("choices")
            or payload.get("allowedValues")
            or []
        )
        options: list[dict[str, Any]] = []
        if isinstance(raw_options, list):
            for index, item in enumerate(raw_options, start=1):
                if isinstance(item, dict):
                    label = str(
                        item.get("label")
                        or item.get("name")
                        or item.get("value")
                        or item.get("id")
                        or ""
                    ).strip()
                    value = item.get("value")
                    if value in (None, ""):
                        value = item.get("id")
                    if value in (None, ""):
                        value = label
                    option_id = str(
                        item.get("id")
                        or item.get("key")
                        or f"option_{index}"
                    ).strip()
                else:
                    label = str(item or "").strip()
                    value = item
                    option_id = f"option_{index}"
                if label:
                    options.append(
                        {
                            "id": option_id[:64],
                            "label": label[:80],
                            "value": value,
                        }
                    )
        return cls(
            code=code,
            label=str(payload.get("label") or "").strip(),
            question=str(payload.get("question") or "").strip(),
            group=str(payload.get("group") or "").strip(),
            patch_key=str(payload.get("patchKey") or "").strip(),
            suggested_value=payload.get("suggestedValue"),
            options=options,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "question": self.question,
            "group": self.group,
            "patchKey": self.patch_key,
            "suggestedValue": self.suggested_value,
            "options": [dict(item) for item in self.options],
        }


@dataclass(slots=True)
class CRMPlaybookResult:
    opportunity_id: int
    protocol: str = ""
    category: str = ""
    crm_ok: bool = True
    next_step: str = ""
    next_question: str = ""
    ready_for_win: bool = False
    credit_status: str = ""
    credit_approved: bool = False
    credit_rejected: bool = False
    sdr_ia_funnel: bool = False
    sdr_ia_defaults: dict[str, Any] = field(default_factory=dict)
    sdr_ia_field_policy: dict[str, Any] = field(default_factory=dict)
    missing: list[CRMPlaybookMissingItem] = field(default_factory=list)

    @classmethod
    def from_payload(
        cls,
        opportunity_id: int,
        payload: Any,
        *,
        category: str,
    ) -> "CRMPlaybookResult":
        source = payload if isinstance(payload, dict) else {}
        opportunity = source.get("opportunity")
        if not isinstance(opportunity, dict):
            opportunity = {}
        credit = source.get("credit")
        if not isinstance(credit, dict):
            credit = {}

        missing: list[CRMPlaybookMissingItem] = []
        raw_missing = source.get("missing")
        if isinstance(raw_missing, list):
            for item in raw_missing:
                normalized = CRMPlaybookMissingItem.from_payload(item)
                if normalized:
                    missing.append(normalized)

        next_question = str(source.get("nextQuestion") or "").strip()
        if not next_question and missing:
            next_question = missing[0].question

        protocol = str(
            source.get("protocol")
            or opportunity.get("protocol")
            or ""
        ).strip()
        raw_defaults = source.get("sdrIaDefaults")
        sdr_ia_defaults = dict(raw_defaults) if isinstance(raw_defaults, dict) else {}
        raw_policy = source.get("sdrIaFieldPolicy")
        sdr_ia_field_policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
        return cls(
            opportunity_id=int(
                source.get("opportunityId")
                or opportunity.get("id")
                or opportunity_id
            ),
            protocol=protocol,
            category=category,
            crm_ok=bool(source.get("crmOk", True)),
            next_step=str(source.get("nextStep") or "").strip(),
            next_question=next_question,
            ready_for_win=bool(source.get("readyForWin")),
            credit_status=str(credit.get("status") or "").strip(),
            credit_approved=bool(credit.get("approved")),
            credit_rejected=bool(credit.get("rejected")),
            sdr_ia_funnel=bool(source.get("sdrIaFunnel")),
            sdr_ia_defaults=sdr_ia_defaults,
            sdr_ia_field_policy=sdr_ia_field_policy,
            missing=missing,
        )

    @property
    def missing_codes(self) -> list[str]:
        return [item.code for item in self.missing]

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunityId": self.opportunity_id,
            "protocol": self.protocol,
            "step": self.next_step or "playbook",
            "crmOk": self.crm_ok,
            "category": self.category,
            "credit": {
                "status": self.credit_status,
                "approved": self.credit_approved,
                "rejected": self.credit_rejected,
            },
            "readyForWin": self.ready_for_win,
            "nextQuestion": self.next_question,
            "missingCodes": self.missing_codes,
            "missing": [item.as_dict() for item in self.missing],
            "sdrIaFunnel": self.sdr_ia_funnel,
            "sdrIaDefaults": dict(self.sdr_ia_defaults),
            "sdrIaFieldPolicy": dict(self.sdr_ia_field_policy),
        }


@dataclass(slots=True)
class SulnetCRMClient:
    base_url: str
    bearer_token: str = ""
    integration_key: str = ""
    timeout: float = 25.0
    _profile_cache: dict[str, Any] | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_settings(cls) -> "SulnetCRMClient":
        if not settings.sulnet_crm_base_url:
            raise CRMIntegrationError("URL da integração com o CRM Sulnet não configurada.")
        if not settings.sulnet_ai_integration_key and not settings.sulnet_crm_bearer_token:
            raise CRMIntegrationError("Credencial da integração com o CRM Sulnet não configurada.")
        return cls(
            base_url=settings.sulnet_crm_base_url.rstrip("/"),
            bearer_token=settings.sulnet_crm_bearer_token,
            integration_key=settings.sulnet_ai_integration_key,
        )

    @staticmethod
    def _sanitize_log_value(value: Any, *, key: str = "") -> Any:
        sensitive_keys = {
            "authorization",
            "token",
            "bearer",
            "password",
            "secret",
            "integrationkey",
            "integration_key",
            "clientcpf",
            "cpf",
            "cnpj",
            "clientphone",
            "phone",
            "clientemail",
            "email",
            "debito_conta",
            "debito_agencia",
            "debito_banco",
            "pdfbase64",
        }
        key_folded = re.sub(r"[^a-z0-9_]+", "", str(key or "").casefold())
        if key_folded in sensitive_keys:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): SulnetCRMClient._sanitize_log_value(
                    item_value,
                    key=str(item_key),
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [
                SulnetCRMClient._sanitize_log_value(item)
                for item in value[:20]
            ]
        text = str(value or "")
        text = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "[DOCUMENTO]", text)
        text = re.sub(r"(?<!\d)\d{5}-?\d{3}(?!\d)", "[CEP]", text)
        text = re.sub(
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            "[EMAIL]",
            text,
        )
        text = re.sub(
            r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+",
            r"\1[REDACTED]",
            text,
        )
        return text[:1200]

    def _auth_headers(
        self,
        auth_mode: Literal["bearer", "integration", "none"],
    ) -> dict[str, str]:
        if auth_mode == "bearer":
            if not self.bearer_token:
                raise CRMIntegrationError(
                    "Token Bearer do CRM Sulnet não configurado.",
                    error_code="CRM_BEARER_NOT_CONFIGURED",
                )
            return {"Authorization": f"Bearer {self.bearer_token}"}
        if auth_mode == "integration":
            if not self.integration_key:
                raise CRMIntegrationError(
                    "Chave de integração do CRM Sulnet não configurada.",
                    error_code="CRM_INTEGRATION_KEY_NOT_CONFIGURED",
                )
            return {"X-Integration-Key": self.integration_key}
        return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        auth_mode: Literal["bearer", "integration", "none"] = "bearer",
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        method_upper = method.upper()
        headers = {
            **self._auth_headers(auth_mode),
            "Accept": "application/json",
            "User-Agent": "Sulnet-Prospecta-Vendedor-IA/3.0",
        }
        if extra_headers:
            headers.update(extra_headers)

        try:
            with httpx.Client(
                timeout=timeout or self.timeout,
                follow_redirects=False,
                headers=headers,
            ) as client:
                response = client.request(
                    method_upper,
                    url,
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            LOG.warning(
                "crm_http_error method=%s path=%s error=%s",
                method_upper,
                path,
                self._sanitize_log_value(str(exc)),
            )
            raise CRMIntegrationError(
                "CRM Sulnet indisponível no momento.",
                method=method_upper,
                path=path,
                error_code="CRM_HTTP_ERROR",
            ) from exc

        LOG.info(
            "crm_http method=%s path=%s status=%s auth=%s",
            method_upper,
            path,
            response.status_code,
            auth_mode,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            LOG.warning(
                "crm_invalid_json method=%s path=%s status=%s",
                method_upper,
                path,
                response.status_code,
            )
            raise CRMIntegrationError(
                f"CRM Sulnet retornou resposta inválida (HTTP {response.status_code}).",
                status_code=response.status_code,
                method=method_upper,
                path=path,
                error_code="CRM_INVALID_JSON",
            ) from exc

        if response.status_code >= 400:
            safe_payload = self._sanitize_log_value(payload)
            error_code = ""
            if isinstance(payload, dict):
                error_code = str(
                    payload.get("code")
                    or payload.get("errorCode")
                    or payload.get("error_code")
                    or ""
                ).strip()
            message = (
                payload.get("error")
                if isinstance(payload, dict)
                else None
            ) or (
                payload.get("message")
                if isinstance(payload, dict)
                else None
            ) or f"Falha no CRM Sulnet (HTTP {response.status_code})."
            LOG.warning(
                "crm_http_failed method=%s path=%s status=%s code=%s body=%s",
                method_upper,
                path,
                response.status_code,
                error_code or "-",
                json.dumps(
                    safe_payload,
                    ensure_ascii=False,
                    default=str,
                )[:1600],
            )
            raise CRMIntegrationError(
                str(self._sanitize_log_value(message))[:600],
                status_code=response.status_code,
                method=method_upper,
                path=path,
                error_code=error_code,
                response_body=safe_payload,
            )
        return payload

    def integration_profile(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._profile_cache is not None and not refresh:
            return dict(self._profile_cache)

        profile: dict[str, Any] = {}
        try:
            result = self._request("GET", "/api/capture/config")
            if isinstance(result, dict):
                profile = dict(result)
        except CRMIntegrationError as exc:
            if exc.status_code != 404:
                raise

        if not profile:
            try:
                result = self._request("GET", "/api/capture/context")
                if isinstance(result, dict):
                    profile = dict(result)
            except CRMIntegrationError as exc:
                if exc.status_code != 404:
                    raise

        # Servidores antigos não publicam apiVersion/capabilities. Marcamos
        # explicitamente como legado para escolher compatibilidade sem tentar
        # cada endpoint no escuro.
        profile.setdefault("apiVersion", 1 if profile else 0)
        profile.setdefault("capabilities", {})
        profile["legacy"] = int(profile.get("apiVersion") or 0) < 2
        self._profile_cache = dict(profile)
        LOG.info(
            "crm_profile api_version=%s app_version=%s legacy=%s",
            profile.get("apiVersion"),
            profile.get("appVersion") or "-",
            profile.get("legacy"),
        )
        return dict(profile)

    def supports(self, capability: str) -> bool:
        profile = self.integration_profile()
        capabilities = profile.get("capabilities")
        return bool(
            isinstance(capabilities, dict)
            and capabilities.get(capability) is True
        )

    def supports_any(self, *capabilities: str) -> bool:
        return any(self.supports(capability) for capability in capabilities)

    @staticmethod
    def normalize_playbook_category(category: str) -> str:
        folded = unicodedata.normalize(
            "NFD",
            str(category or "").strip().casefold(),
        )
        folded = "".join(
            ch for ch in folded
            if unicodedata.category(ch) != "Mn"
        )
        compact = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
        if compact in {"fibra", "fiber", "ftth", "internet"}:
            return "fibra"
        if compact in {
            "telefonia-movel",
            "telefonia-mob",
            "movel",
            "mobile",
            "sulmovel",
        }:
            return "telefonia-movel"
        raise CRMIntegrationError(
            "Categoria de playbook inválida.",
            error_code="CRM_PLAYBOOK_CATEGORY_INVALID",
        )

    def playbook(
        self,
        opportunity_id: int,
        *,
        category: str,
    ) -> CRMPlaybookResult:
        normalized_category = self.normalize_playbook_category(category)
        result = self._request(
            "GET",
            f"/api/integracoes/agent/v1/opportunities/{opportunity_id}/playbook",
            params={"category": normalized_category},
        )
        return CRMPlaybookResult.from_payload(
            opportunity_id,
            result,
            category=normalized_category,
        )

    def playbook_template(self, *, category: str) -> dict[str, Any]:
        normalized_category = self.normalize_playbook_category(category)
        result = self._request(
            "GET",
            "/api/integracoes/agent/v1/playbook/templates",
            params={"category": normalized_category},
        )
        if isinstance(result, dict):
            return dict(result)
        return {
            "category": normalized_category,
            "items": [dict(item) for item in result if isinstance(item, dict)],
        }

    def generate_agent_contract(
        self,
        opportunity_id: int,
        *,
        send_signature: bool = True,
        require_ready_for_win: bool = True,
    ) -> dict[str, Any]:
        """Gera contrato e, quando solicitado, já cria/reutiliza a assinatura.

        O endpoint agent é a fonte de verdade atual para fechamento SDR IA.
        Não dependemos do capability profile para chamá-lo porque o CRM pode
        publicar o endpoint antes de atualizar /api/capture/config/context.
        O chamador trata 404/405/501 como compatibilidade com CRM legado.
        """
        result = self._request(
            "POST",
            (
                f"/api/integracoes/agent/v1/opportunities/"
                f"{opportunity_id}/contract/generate"
            ),
            json_body={
                "sendSignature": bool(send_signature),
                "requireReadyForWin": bool(require_ready_for_win),
            },
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}

    def send_contract_webhook(
        self,
        *,
        protocol: str,
        file_name: str,
        pdf_base64: str,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/webhook/contrato",
            json_body={
                "protocol": protocol,
                "fileName": file_name,
                "pdfBase64": pdf_base64,
            },
            auth_mode="integration",
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}

    def context(self) -> dict[str, Any]:
        return self.integration_profile()

    def config(self) -> dict[str, Any]:
        return self.integration_profile()

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

    def _stage_id_by_name(self, *names: str) -> int | None:
        try:
            context = self.context()
        except CRMIntegrationError:
            return None
        stages = context.get("stages")
        if not isinstance(stages, list):
            return None
        wanted = {
            self._fold_catalog_text(name)
            for name in names
            if str(name or "").strip()
        }
        for item in stages:
            if not isinstance(item, dict):
                continue
            name = self._fold_catalog_text(str(item.get("name") or ""))
            if name in wanted:
                try:
                    return int(item.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    def _data_stage_id(self) -> int | None:
        return self._stage_id_by_name("DADOS E PLANO")

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
                # Só a contagem: o payload pode carregar dados do cliente.
                LOG.warning(
                    "crm_plan_ambiguous candidates=%s",
                    len(products),
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
        if self.supports("leadUpdate"):
            result = self._request(
                "PATCH",
                f"/api/capture/leads/{opportunity_id}",
                json_body=payload,
            )
            return result if isinstance(result, dict) else {}

        LOG.warning(
            "crm_legacy_generic_patch opportunity_id=%s",
            opportunity_id,
        )
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

        product_info = current.get("productInfo")
        if not isinstance(product_info, dict):
            product_info = {}

        ai_closing = current.get("aiClosing")
        if not isinstance(ai_closing, dict):
            ai_closing = product_info.get("aiClosing")
        if not isinstance(ai_closing, dict):
            ai_closing = {}

        category_form = product_info.get("categoryForm")
        if not isinstance(category_form, dict):
            category_form = {}
        current_values = category_form.get("values")
        if not isinstance(current_values, dict):
            current_values = {}
        merged_values = dict(current_values)
        merged_values.update(values)

        current_files = category_form.get("files")
        if not isinstance(current_files, dict):
            current_files = {}

        payload: dict[str, Any] = {
            "productInfo": {
                **{
                    key: value
                    for key, value in product_info.items()
                    if key not in {"aiClosing", "categoryForm"}
                },
                "aiClosing": dict(ai_closing),
                "categoryForm": {
                    **({"categoryId": int(category_id)} if category_id else {}),
                    "values": merged_values,
                    # Preserva anexos já existentes; a IA não cria documentos/selfies.
                    "files": dict(current_files),
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

    @staticmethod
    def _playbook_patch_payload(
        patch_key: str,
        value: Any,
    ) -> dict[str, Any]:
        raw = str(patch_key or "").strip()
        if not raw:
            raise CRMIntegrationError(
                "Playbook não informou patchKey para persistir o campo.",
                error_code="CRM_PLAYBOOK_PATCH_KEY_MISSING",
            )

        normalized = raw.replace("[", ".").replace("]", "")
        normalized = normalized.lstrip("$. /")
        parts = [
            item.strip()
            for item in re.split(r"[./]+", normalized)
            if item.strip() and item.strip() != "$"
        ]
        if not parts or len(parts) > 10:
            raise CRMIntegrationError(
                "patchKey inválido retornado pelo playbook.",
                error_code="CRM_PLAYBOOK_PATCH_KEY_INVALID",
            )
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", item)
            for item in parts
        ):
            raise CRMIntegrationError(
                "patchKey contém segmento não permitido.",
                error_code="CRM_PLAYBOOK_PATCH_KEY_INVALID",
            )

        nested: Any = value
        for item in reversed(parts):
            nested = {item: nested}
        return nested if isinstance(nested, dict) else {}

    @staticmethod
    def _deep_merge_dict(
        base: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = SulnetCRMClient._deep_merge_dict(
                    dict(merged[key]),
                    value,
                )
            else:
                merged[key] = value
        return merged

    def update_playbook_field(
        self,
        opportunity_id: int,
        *,
        patch_key: str,
        value: Any,
    ) -> dict[str, Any]:
        payload = self._playbook_patch_payload(patch_key, value)
        current = self.get_opportunity(opportunity_id)
        if isinstance(current, dict):
            payload = self._deep_merge_dict(current, payload)

        # Nunca devolvemos identificadores/estado capturados pelo GET no PATCH.
        # Preservamos apenas os ramos que o patchKey pediu para alterar.
        patch_root = next(iter(self._playbook_patch_payload(patch_key, value)))
        payload = {patch_root: payload.get(patch_root)}

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
        # Pacote oficial SDR IA: o catálogo de produtos é /api/catalog/products.
        # Mantemos /api/capture/products apenas como compatibilidade com versões
        # futuras/alternativas que publiquem essa capability.
        try:
            return self._catalog_products_fallback(query)
        except CRMIntegrationError as exc:
            if exc.status_code not in {404, 405} or not self.supports("products"):
                raise

        result = self._request(
            "GET",
            "/api/capture/products",
            params={"q": query} if query else None,
        )
        return result if isinstance(result, list) else []

    def mark_won(
        self,
        opportunity_id: int,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request_key = str(idempotency_key or uuid.uuid4())
        try:
            uuid.UUID(request_key)
        except (ValueError, AttributeError) as exc:
            raise CRMIntegrationError(
                "Idempotency-Key inválido para marcar ganho.",
                error_code="CRM_IDEMPOTENCY_KEY_INVALID",
            ) from exc

        idempotency_headers = {"Idempotency-Key": request_key}
        if self.supports("markWon"):
            result = self._request(
                "POST",
                f"/api/capture/leads/{opportunity_id}/win",
                json_body={},
                extra_headers=idempotency_headers,
            )
            return result if isinstance(result, dict) else {}

        # CRM legado: usa a operação genérica já homologada. Quando o CRM
        # publicar apiVersion >= 2, este caminho deixa de ser usado.
        won_stage_id = self._stage_id_by_name("GANHO")
        LOG.warning(
            "crm_win_legacy_generic opportunity_id=%s stage_id=%s",
            opportunity_id,
            won_stage_id or "not-published",
        )
        win_payload: dict[str, Any] = {
            "status": "won",
            "historyAction": "Venda do Vendedor IA validada e marcada como ganha.",
        }
        if won_stage_id:
            win_payload["stageId"] = won_stage_id

        updated = self._request(
            "PATCH",
            f"/api/opportunities/{opportunity_id}",
            json_body=win_payload,
            extra_headers=idempotency_headers,
        )
        if not isinstance(updated, dict):
            updated = {}

        return {
            "alreadyWon": str(updated.get("status") or "").lower() == "won",
            "opportunity": updated,
            "backofficeOpportunityId": (
                (updated.get("backofficeCopy") or {}).get("id")
                if isinstance(updated.get("backofficeCopy"), dict)
                else None
            ),
            "contract": {"status": "aguardando_backoffice"},
        }

    def get_status(self, opportunity_id: int) -> dict[str, Any]:
        if self.supports("status"):
            result = self._request(
                "GET",
                f"/api/capture/leads/{opportunity_id}/status",
            )
            return result if isinstance(result, dict) else {}

        # Legado não expõe estado consolidado do contrato. Mantemos apenas o
        # estado comercial sem gerar 404 em loop; ao atualizar o CRM, o profile
        # v2 passa automaticamente a usar /status.
        opportunity = self.get_opportunity(opportunity_id)
        return {
            "opportunity": opportunity,
            "contract": {
                "status": (
                    "aguardando_backoffice"
                    if str(opportunity.get("status") or "").lower() == "won"
                    else "aguardando_fechamento_crm"
                )
            },
        }

    def get_contract(self, opportunity_id: int) -> dict[str, Any]:
        if not self.supports("contract"):
            return {"status": "aguardando_backoffice"}
        result = self._request(
            "GET",
            f"/api/capture/leads/{opportunity_id}/contract",
        )
        return result if isinstance(result, dict) else {}

    def start_contract(self, opportunity_id: int) -> dict[str, Any]:
        if not self.supports("contract"):
            return {"status": "aguardando_backoffice"}
        result = self._request(
            "POST",
            f"/api/capture/leads/{opportunity_id}/contract",
            json_body={},
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}
