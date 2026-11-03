"""Uma única fonte de preços: tabela de planos → Guardião, base e prompt.

Regressão: o Guardião recebia os planos residenciais sem preço e o 650 Mega com
a cidade "ALL_20", e barrava respostas com o preço certo (conversa 238).
"""

from __future__ import annotations

import re

from app.services.ai_sdr_policy import (
    CORPORATE_PLANS,
    FTTH_PLANS,
    MOBILE_PLANS,
    SERVICE_CITIES,
    SULIA_SYSTEM_PROMPT,
    SYSTEM_KNOWLEDGE_ITEMS,
    approved_catalog,
)
from app.services.ai_supervisor import _catalog_snapshot


def _brl(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def test_guardian_catalog_has_residential_prices_and_real_cities():
    catalog = _catalog_snapshot()
    by_code = {row["code"]: row for row in catalog["residential"]}

    assert by_code["FTTH_800"]["price_with_fidelity"] == 119.90
    assert by_code["FTTH_800"]["price_without_fidelity"] == 159.90
    assert by_code["FTTH_650"]["price_with_fidelity"] == 99.90
    assert "Santo Ângelo" in by_code["FTTH_650"]["cities"]
    assert by_code["FTTH_650"]["cities"] == SERVICE_CITIES
    for row in catalog["residential"] + catalog["corporate"] + catalog["mobile"]:
        assert row.get("price_with_fidelity", row.get("price")) is not None
    assert all(row["gb"] for row in catalog["mobile"])


def test_catalog_is_built_from_the_plan_tables():
    catalog = approved_catalog()
    assert [row["code"] for row in catalog["residential"]] == [p["code"] for p in FTTH_PLANS]
    assert [row["price"] for row in catalog["corporate"]] == [p["price"] for p in CORPORATE_PLANS]
    assert [row["price"] for row in catalog["mobile"]] == [p["price"] for p in MOBILE_PLANS]


def _knowledge(title: str) -> str:
    return next(item["content"] for item in SYSTEM_KNOWLEDGE_ITEMS if item["title"] == title)


def test_knowledge_base_prices_match_plan_table():
    plans = {plan["download_mbps"]: plan for plan in FTTH_PLANS}
    for speed, title in ((800, "FTTH 800 Mega"), (650, "FTTH 650 Mega")):
        text = _knowledge(title)
        assert _brl(plans[speed]["price_fidelity"]) in text
        assert _brl(plans[speed]["price_no_fidelity"]) in text
    small = _knowledge("FTTH 500 e 250 Mega")
    for speed in (500, 250):
        assert _brl(plans[speed]["price_fidelity"]) in small
        assert _brl(plans[speed]["price_no_fidelity"]) in small
    mobile = _knowledge("Sulmóvel - planos confirmados por contrato")
    for plan in MOBILE_PLANS:
        assert _brl(plan["price"]) in mobile


def test_prompt_prices_exist_in_plan_table():
    known = {
        _brl(value)
        for plan in FTTH_PLANS
        for value in (plan["price_fidelity"], plan.get("price_no_fidelity"))
        if value
    }
    known |= {_brl(plan["price"]) for plan in CORPORATE_PLANS + MOBILE_PLANS}
    known |= {_brl(plan.get("additional_router_monthly_price") or 0) for plan in FTTH_PLANS}
    known |= {"860,00"}
    # Benefício de mensalidades grátis (1, 2 ou 3 mensalidades) também deriva da tabela.
    known |= {
        _brl(plan["price_fidelity"] * months)
        for plan in FTTH_PLANS
        for months in (1, 2, 3)
    }
    cited = set(re.findall(r"R\$ ?(\d{2,3},\d{2})", SULIA_SYSTEM_PROMPT))
    assert cited, "o prompt deveria citar preços"
    assert cited <= known, f"preços no prompt fora da tabela: {sorted(cited - known)}"
