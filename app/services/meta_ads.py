"""Gestor de Tráfego Pago: leitura da conta de anúncios Meta (somente leitura).

O token (usuário de sistema da BM03) vem de variável de ambiente e nunca é
gravado em banco, log ou mensagem de erro.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"
_ACCOUNT_RE = re.compile(r"^act_[0-9]{5,20}$")
_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+$")
_ACCOUNT_STATUS = {
    1: "Ativa", 2: "Desativada", 3: "Pendente de pagamento", 7: "Em análise de risco",
    8: "Em período de carência", 9: "Em período de carência", 100: "Fechamento pendente",
    101: "Fechada", 201: "Qualquer ativa", 202: "Qualquer fechada",
}


class MetaAdsError(Exception):
    """Erro seguro para exibir ao usuário (nunca contém o token)."""


@dataclass
class MetaAdsSettings:
    token: str
    account_id: str
    app_id: str
    version: str

    @classmethod
    def from_env(cls) -> "MetaAdsSettings":
        return cls(
            token=os.getenv("META_ADS_ACCESS_TOKEN", "").strip(),
            account_id=os.getenv("META_ADS_ACCOUNT_ID", "").strip(),
            app_id=os.getenv("META_ADS_APP_ID", "").strip(),
            version=os.getenv("META_ADS_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip() or DEFAULT_GRAPH_VERSION,
        )


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class ConnectionReport:
    checks: list[Check] = field(default_factory=list)
    account: dict[str, Any] = field(default_factory=dict)
    accessible_accounts: list[dict[str, str]] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def _get(client: httpx.Client, settings: MetaAdsSettings, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = client.get(
            f"{GRAPH_BASE}/{settings.version}/{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {settings.token}"},
        )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MetaAdsError("Não foi possível falar com a Meta. Verifique a conexão.") from exc
    if not isinstance(data, dict):
        raise MetaAdsError("Resposta inesperada da Meta.")
    error = data.get("error")
    if response.status_code >= 300 or error:
        code = error.get("code") if isinstance(error, dict) else None
        if code == 190:
            raise MetaAdsError("Token inválido, expirado ou anulado.")
        if code in (10, 200, 299):
            raise MetaAdsError("O token não tem permissão para este recurso.")
        raise MetaAdsError("A Meta recusou a consulta (código %s)." % (code if isinstance(code, int) else "desconhecido"))
    return data


def check_connection(settings: MetaAdsSettings | None = None, *, client: httpx.Client | None = None) -> ConnectionReport:
    """Confere token, conta de anúncios e isolamento. Só faz leituras (GET)."""
    settings = settings or MetaAdsSettings.from_env()
    report = ConnectionReport()

    missing = [name for name, value in (
        ("META_ADS_ACCESS_TOKEN", settings.token),
        ("META_ADS_ACCOUNT_ID", settings.account_id),
    ) if not value]
    if missing:
        report.checks.append(Check("Variáveis de ambiente", False, "Faltando: " + ", ".join(missing)))
        return report
    if not _ACCOUNT_RE.match(settings.account_id) or not _VERSION_RE.match(settings.version):
        report.checks.append(Check("Variáveis de ambiente", False, "META_ADS_ACCOUNT_ID deve ser do tipo act_123… e a versão do tipo v21.0."))
        return report
    report.checks.append(Check("Variáveis de ambiente", True, "Token e conta configurados."))

    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    try:
        try:
            me = _get(client, settings, "me", {"fields": "id,name"})
            report.checks.append(Check("Token válido", True, "Usuário de sistema: %s" % me.get("name", "—")))
        except MetaAdsError as exc:
            report.checks.append(Check("Token válido", False, str(exc)))
            return report

        try:
            perms = _get(client, settings, "me/permissions")
            granted = sorted(
                p.get("permission", "") for p in perms.get("data", [])
                if isinstance(p, dict) and p.get("status") == "granted"
            )
            needed = {"ads_management", "ads_read"}
            missing_perms = sorted(needed - set(granted))
            report.checks.append(Check(
                "Permissões do token", not missing_perms,
                "Concedidas: " + (", ".join(granted) or "nenhuma")
                + ("" if not missing_perms else " · faltando: " + ", ".join(missing_perms)),
            ))
        except MetaAdsError as exc:
            report.checks.append(Check("Permissões do token", False, str(exc)))

        try:
            account = _get(client, settings, settings.account_id, {
                "fields": "name,account_status,currency,timezone_name,amount_spent,spend_cap",
            })
            report.account = account
            status = _ACCOUNT_STATUS.get(account.get("account_status"), "Desconhecido")
            report.checks.append(Check(
                "Conta de anúncios acessível", account.get("account_status") == 1,
                "%s · %s · %s · %s" % (account.get("name", "—"), status, account.get("currency", "—"), account.get("timezone_name", "—")),
            ))
        except MetaAdsError as exc:
            report.checks.append(Check("Conta de anúncios acessível", False, str(exc)))

        try:
            pages = _get(client, settings, f"{settings.account_id}/promote_pages", {"fields": "id,name", "limit": 25})
            rows = [r for r in pages.get("data", []) if isinstance(r, dict)]
            names = ", ".join(str(r.get("name", "—")) for r in rows)
            report.checks.append(Check(
                "Página disponível para anúncios", bool(rows),
                names if rows else "Nenhuma Página liberada para esta conta de anúncios.",
            ))
        except MetaAdsError as exc:
            report.checks.append(Check("Página disponível para anúncios", False, str(exc)))

        try:
            listing = _get(client, settings, "me/adaccounts", {"fields": "id,name", "limit": 50})
            rows = [r for r in listing.get("data", []) if isinstance(r, dict)]
            report.accessible_accounts = [{"id": str(r.get("id", "")), "name": str(r.get("name", ""))} for r in rows]
            only_ours = len(rows) == 1 and rows[0].get("id") == settings.account_id
            report.checks.append(Check(
                "Isolamento (só enxerga a conta de teste)", only_ours,
                "O token enxerga %d conta(s) de anúncios." % len(rows),
            ))
        except MetaAdsError as exc:
            report.checks.append(Check("Isolamento (só enxerga a conta de teste)", False, str(exc)))
    finally:
        if owns_client:
            client.close()
    return report


PERIODS = {
    "hoje": ("today", "Hoje"),
    "ontem": ("yesterday", "Ontem"),
    "7d": ("last_7d", "Últimos 7 dias"),
    "14d": ("last_14d", "Últimos 14 dias"),
    "30d": ("last_30d", "Últimos 30 dias"),
    "total": ("maximum", "Desde o início"),
}
_CONVERSATION_ACTION = "onsite_conversion.messaging_conversation_started_7d"


@dataclass
class CampaignRow:
    id: str
    name: str
    status: str
    objective: str
    daily_budget: float | None
    spend: float
    impressions: int
    clicks: int
    ctr: float
    conversations: int
    cost_per_conversation: float | None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_campaigns(
    period: str = "7d", settings: MetaAdsSettings | None = None, *, client: httpx.Client | None = None
) -> list[CampaignRow]:
    """Campanhas da conta com gasto e conversas no WhatsApp no período. Só leitura."""
    settings = settings or MetaAdsSettings.from_env()
    if not settings.token or not _ACCOUNT_RE.match(settings.account_id) or not _VERSION_RE.match(settings.version):
        raise MetaAdsError("Conexão com a Meta não configurada.")
    preset = PERIODS.get(period, PERIODS["7d"])[0]
    fields = (
        "id,name,effective_status,objective,daily_budget,"
        f"insights.date_preset({preset}){{spend,impressions,clicks,ctr,actions}}"
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        data = _get(client, settings, f"{settings.account_id}/campaigns", {"fields": fields, "limit": 100})
    finally:
        if owns_client:
            client.close()
    rows: list[CampaignRow] = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        insight = ((item.get("insights") or {}).get("data") or [{}])[0]
        spend = _num(insight.get("spend"))
        conversations = int(sum(
            _num(a.get("value")) for a in insight.get("actions", [])
            if isinstance(a, dict) and a.get("action_type") == _CONVERSATION_ACTION
        ))
        budget = _num(item.get("daily_budget"))
        rows.append(CampaignRow(
            id=str(item.get("id", "")),
            name=str(item.get("name", "—")),
            status=str(item.get("effective_status", "—")),
            objective=str(item.get("objective", "—")),
            daily_budget=budget / 100 if budget else None,
            spend=spend,
            impressions=int(_num(insight.get("impressions"))),
            clicks=int(_num(insight.get("clicks"))),
            ctr=_num(insight.get("ctr")),
            conversations=conversations,
            cost_per_conversation=(spend / conversations) if conversations else None,
        ))
    rows.sort(key=lambda r: r.spend, reverse=True)
    return rows
