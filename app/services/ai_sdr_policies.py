"""Políticas comerciais fixas do SDR (catálogo, jornada, entrada).

O modelo só classifica e redige; estas regras reescrevem a decisão depois.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from app.models import Message
from app.services.ai_quick_replies import QuickReply
from app.services import ai_campaign_black as black
from app.services.ai_sdr_models import AIDecision, QualificationData
from app.services.ai_sdr_policy import (
    CENTRAL_REDIRECT_MESSAGE,
    CORPORATE_PLANS,
    FTTH_PLANS,
    MOBILE_PLANS,
    PLAN_CHANGE_SERVICE_PHONE,
    SERVICE_CITIES,
)


def _fold_sales_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_EXISTING_CUSTOMER_QUESTION_CUES = (
    "voce ja e cliente sulnet",
    "ja e cliente sulnet",
    "voce ja e nosso cliente",
    "voce ja possui algum servico da sulnet",
    "ja possui algum servico da sulnet",
    "possui algum servico da sulnet",
    "voce tem algum servico da sulnet",
    "tem algum servico da sulnet",
)


def _asks_existing_customer(value: str | None) -> bool:
    folded = _fold_sales_text(value)
    return bool(folded) and any(
        cue in folded for cue in _EXISTING_CUSTOMER_QUESTION_CUES
    )


def _product_kind_from_text(
    value: str | None,
    *,
    previous_outbound: str | None = None,
) -> str:
    folded = _fold_sales_text(value)
    previous = _fold_sales_text(previous_outbound)
    if not folded:
        return ""

    mobile = {
        "movel",
        "telefonia movel",
        "plano movel",
        "planos moveis",
        "sulmovel",
        "celular",
    }
    internet = {
        "internet",
        "fibra",
        "fibra optica",
        "plano de internet",
        "planos de internet",
    }
    if folded in mobile:
        return "MOBILE"
    if folded in internet:
        return "INTERNET"

    product_question = (
        "internet" in previous
        and any(token in previous for token in ("telefonia movel", "movel"))
    )
    if product_question:
        if folded in {"internet", "fibra", "fibra optica"}:
            return "INTERNET"
        if folded in {"movel", "telefonia movel", "sulmovel", "celular"}:
            return "MOBILE"
    return ""


def _confirmed_facts_from_history(rows: list[Message] | list[dict]) -> dict:
    facts: dict[str, object] = {
        "existing_customer": "",
        "customer_type": "",
        "product_kind": "",
        "desired_product": "",
        "city": "",
        "answered_questions": [],
    }
    answered: set[str] = set()
    previous_outbound = ""

    for row in rows:
        if isinstance(row, dict):
            direction = str(row.get("direction") or "").upper()
            body = str(row.get("body") or "")
        else:
            direction = str(row.direction or "").upper()
            body = str(row.body or "")

        if direction == "OUTBOUND":
            previous_outbound = body
            continue
        if direction != "INBOUND":
            continue

        existing = _existing_customer_from_text(
            body,
            previous_outbound=previous_outbound,
        )
        if existing:
            facts["existing_customer"] = existing
            answered.add("EXISTING_CUSTOMER")

        customer_type = _customer_type_from_text(body)
        if customer_type:
            facts["customer_type"] = customer_type
            answered.add("CUSTOMER_TYPE")

        product_kind = _product_kind_from_text(
            body,
            previous_outbound=previous_outbound,
        )
        if product_kind:
            facts["product_kind"] = product_kind
            facts["desired_product"] = (
                "Internet" if product_kind == "INTERNET" else "Telefonia Móvel"
            )
            answered.add("PRODUCT")

        folded = _fold_sales_text(body)
        previous_folded = _fold_sales_text(previous_outbound)
        asks_city = any(
            cue in previous_folded
            for cue in (
                "qual e a sua cidade",
                "qual sua cidade",
                "em qual cidade",
                "qual cidade",
            )
        )
        for service_city in SERVICE_CITIES:
            city_key = _fold_sales_text(service_city)
            if folded == city_key or (asks_city and city_key in folded):
                facts["city"] = service_city
                answered.add("CITY")
                break

    facts["answered_questions"] = sorted(answered)
    return facts


def _existing_customer_from_text(
    value: str | None,
    *,
    previous_outbound: str | None = None,
) -> str:
    folded = _fold_sales_text(value)
    previous = _fold_sales_text(previous_outbound)
    if not folded:
        return ""

    negative_exact = {
        "nao sou cliente",
        "ainda nao sou cliente",
        "nunca fui cliente",
    }
    negative_sulnet_cues = (
        "nao sou cliente sulnet",
        "nao sou cliente da sulnet",
        "nao tenho sulnet",
        "nao sou da sulnet",
    )
    positive_exact = {
        "sou cliente",
        "ja sou cliente",
    }
    positive_sulnet_cues = (
        "sou cliente sulnet",
        "sou cliente da sulnet",
        "ja sou cliente sulnet",
        "ja sou cliente da sulnet",
        "ja tenho sulnet",
        "tenho sulnet",
    )
    if folded in negative_exact or any(cue in folded for cue in negative_sulnet_cues):
        return "NAO_CLIENTE"
    if folded in positive_exact or any(cue in folded for cue in positive_sulnet_cues):
        return "CLIENTE"

    status_question = _asks_existing_customer(previous_outbound)
    if status_question:
        if folded in {"sim", "sim sou", "sou", "ja sou", "sou sim"}:
            return "CLIENTE"
        if folded in {"nao", "nao sou", "ainda nao", "nao ainda"}:
            return "NAO_CLIENTE"
    return ""


def _existing_customer_from_context(context: dict) -> str:
    facts = context.get("confirmed_facts") or {}
    fact_value = str(facts.get("existing_customer") or "").upper()
    if fact_value in {"CLIENTE", "NAO_CLIENTE"}:
        return fact_value

    previous = _last_outbound_text(context)
    latest = _existing_customer_from_text(
        str(context.get("latest_customer_message") or ""),
        previous_outbound=previous,
    )
    if latest:
        return latest

    confirmed = str(
        (context.get("lead") or {}).get("confirmed_existing_customer") or ""
    ).upper()
    if confirmed in {"CLIENTE", "NAO_CLIENTE"}:
        return confirmed

    journey_value = str(
        _journey_memory_from_context(context).get("existing_customer") or ""
    ).upper()
    if journey_value in {"CLIENTE", "NAO_CLIENTE"}:
        return journey_value

    previous_outbound = ""
    reconstructed = ""
    for row in context.get("history") or []:
        direction = str(row.get("direction") or "").upper()
        body = str(row.get("body") or "")
        if direction == "OUTBOUND":
            previous_outbound = body
            continue
        if direction != "INBOUND":
            continue
        value = _existing_customer_from_text(
            body,
            previous_outbound=previous_outbound,
        )
        if value:
            reconstructed = value
    return reconstructed


def _customer_type_from_text(value: str | None) -> str:
    folded = _fold_sales_text(value)
    if not folded:
        return ""

    company_exact = {
        "empresa",
        "para empresa",
        "plano empresa",
        "plano empresarial",
        "plano para empresa",
        "corporativo",
        "pj",
        "pessoa juridica",
    }
    company_cues = (
        "minha empresa",
        "nossa empresa",
        "meu negocio",
        "meu comercio",
        "minha loja",
        "meu escritorio",
        "quero para empresa",
        "preciso para empresa",
        "internet para empresa",
        "plano corporativo",
        "agora e para empresa",
    )
    residential_exact = {
        "casa",
        "para casa",
        "residencial",
        "plano residencial",
        "internet residencial",
    }
    residential_cues = (
        "minha casa",
        "minha residencia",
        "meu apartamento",
        "quero para casa",
        "preciso para casa",
        "internet para casa",
        "agora e para casa",
    )

    if folded in company_exact or any(cue in folded for cue in company_cues):
        return "EMPRESA"
    if folded in residential_exact or any(cue in folded for cue in residential_cues):
        return "RESIDENCIAL"
    return ""


def _customer_type_from_context(context: dict) -> str:
    facts = context.get("confirmed_facts") or {}
    fact_value = str(facts.get("customer_type") or "").upper()
    if fact_value in {"EMPRESA", "RESIDENCIAL"}:
        return fact_value

    latest_type = _customer_type_from_text(
        str(context.get("latest_customer_message") or "")
    )
    if latest_type:
        return latest_type

    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        value = _customer_type_from_text(str(row.get("body") or ""))
        if value:
            return value

    confirmed = str(
        (context.get("lead") or {}).get("confirmed_customer_type") or ""
    ).upper()
    return confirmed if confirmed in {"EMPRESA", "RESIDENCIAL"} else ""


def _customer_inbound_text(context: dict) -> str:
    parts = []
    for row in context.get("history") or []:
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        parts.append(_fold_sales_text(str(row.get("body") or "")))
    return " ".join(part for part in parts if part).strip()


def _numbers_in_text(value: str | None) -> set[str]:
    return set(re.findall(r"\d+", _fold_sales_text(value)))


def _sanitize_unconfirmed_customer_facts(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    inbound = _customer_inbound_text(context)
    qualification = decision.qualification
    confirmed_customer_type = _customer_type_from_context(context)
    qualification.customer_type = (
        confirmed_customer_type if confirmed_customer_type else "NAO_INFORMADO"
    )
    confirmed_existing_customer = _existing_customer_from_context(context)
    qualification.existing_customer = (
        confirmed_existing_customer if confirmed_existing_customer else "NAO_INFORMADO"
    )

    provider = _fold_sales_text(qualification.current_provider)
    if provider and provider not in inbound:
        original_provider = qualification.current_provider or ""
        qualification.current_provider = None
        for field_name in ("reply_text", "summary", "next_action", "proposal_summary"):
            value = getattr(decision, field_name, "") or ""
            if original_provider and original_provider.casefold() in value.casefold():
                value = re.sub(
                    re.escape(original_provider),
                    "seu provedor atual",
                    value,
                    flags=re.IGNORECASE,
                )
                setattr(decision, field_name, value)

    speed_numbers = _numbers_in_text(qualification.current_speed)
    inbound_numbers = _numbers_in_text(inbound)
    if qualification.current_speed and speed_numbers and speed_numbers.isdisjoint(inbound_numbers):
        qualification.current_speed = None

    price_numbers = _numbers_in_text(qualification.current_price)
    if qualification.current_price and price_numbers and price_numbers.isdisjoint(inbound_numbers):
        qualification.current_price = None

    return decision


_SECONDARY_DECLINES = {
    "nao",
    "nao obrigado",
    "nao obrigada",
    "agora nao",
    "prefiro nao",
    "dispenso",
}

_AUXILIARY_QUESTION_CUES = (
    "quer que eu confira",
    "quer que eu verifique",
    "quer que eu explique",
    "quer que eu tambem",
    "quer saber como",
    "posso conferir",
    "posso verificar",
    "posso explicar",
    "como funciona",
    "como acessar",
    "sulnet play",
    "beneficio",
    "beneficios",
    "conteudo",
    "acesso",
)


def _plan_from_sales_context(decision: AIDecision, context: dict):
    code = (decision.offer_plan_code or "").upper()
    if code:
        plan = next(
            (
                item
                for item in FTTH_PLANS
                if code == item["code"] or str(item["download_mbps"]) in code
            ),
            None,
        )
        if plan:
            return plan

    for row in reversed(context.get("history") or []):
        body = str(row.get("body") or "")
        match = re.search(r"\b(250|500|650|800)\s*mega\b", body, re.I)
        if not match:
            continue
        speed = int(match.group(1))
        plan = next(
            (item for item in FTTH_PLANS if item["download_mbps"] == speed),
            None,
        )
        if plan:
            return plan
    return None


def _normalized_city(value: str | None) -> str:
    return _fold_sales_text(value)


def _city_from_sales_context(decision: AIDecision, context: dict) -> str:
    fact_city = str((context.get("confirmed_facts") or {}).get("city") or "").strip()
    if fact_city:
        return fact_city

    lead_city = str((context.get("lead") or {}).get("city") or "").strip()
    if lead_city:
        return lead_city

    qualification_city = str(decision.qualification.city or "").strip()
    if qualification_city:
        return qualification_city

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    for service_city in SERVICE_CITIES:
        city_key = _normalized_city(service_city)
        if latest == city_key or city_key in latest:
            return service_city
    return ""


def _plan_available_in_city(plan: dict, city: str) -> bool:
    city_key = _normalized_city(city)
    if not city_key:
        return False

    service_city_keys = {_normalized_city(item) for item in SERVICE_CITIES}
    if city_key not in service_city_keys:
        return False

    cities = plan.get("cities")
    if cities == "ALL_20":
        return True
    return city_key in {_normalized_city(str(item)) for item in (cities or [])}


def _eligible_plans_for_city(city: str) -> list[dict]:
    plans = [plan for plan in FTTH_PLANS if _plan_available_in_city(plan, city)]
    return sorted(plans, key=lambda item: int(item["download_mbps"]), reverse=True)


def _requested_plan_speed(value: str | None) -> int | None:
    folded = _fold_sales_text(value)
    if not folded:
        return None

    match = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", folded)
    if not match:
        return None

    speed = int(match.group(1))
    request_cues = (
        "quero",
        "queria",
        "gostaria",
        "interesse",
        "preciso",
        "tem ",
        "voces tem",
        "plano",
        "contratar",
        "oferece",
        "disponivel",
        "trabalham com",
    )
    current_plan_cues = (
        "eu tenho",
        "tenho hoje",
        "uso ",
        "meu plano",
        "minha internet",
        "atualmente",
        "hoje tenho",
    )

    if any(cue in folded for cue in current_plan_cues) and not any(
        cue in folded for cue in request_cues
    ):
        return None

    compact = folded in {
        f"{speed} mega",
        f"{speed} mb",
        f"{speed} mbps",
    }
    if compact or any(cue in folded for cue in request_cues):
        return speed
    return None


def _format_brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _last_outbound_text(context: dict) -> str:
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            return str(row.get("body") or "")
    return ""


def _looks_like_catalog_request(value: str | None) -> bool:
    folded = _fold_sales_text(value)
    if not folded:
        return False
    cues = (
        "todos os planos",
        "todos planos",
        "quais planos",
        "lista de planos",
        "catalogo",
        "planos disponiveis",
        "opcoes de planos",
        "planos da sulnet",
        "planos voces tem",
        "planos que voces tem",
    )
    if any(cue in folded for cue in cues):
        return True
    return (
        "planos" in folded
        and ("precos" in folded or "preco" in folded)
        and ("velocidades" in folded or "velocidade" in folded or "promocoes" in folded)
    )


def _reset_catalog_handoff(decision: AIDecision) -> None:
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True


def _show_catalog_root(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Claro! Tenho o catálogo comercial vigente da Sulnet com os valores confirmados. "
        "O que você quer consultar primeiro?"
    )
    decision.quick_replies = [
        QuickReply(id="catalogo_fibra", label="Fibra óptica"),
        QuickReply(id="catalogo_sulmovel", label="Móvel"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.summary = "Cliente solicitou o catálogo de planos da Sulnet."
    decision.next_action = "Cliente escolher entre Fibra óptica e Sulmóvel."
    _reset_catalog_handoff(decision)
    return decision


def _show_mobile_catalog_page(
    decision: AIDecision,
    *,
    page: int,
) -> AIDecision:
    decision.qualification.desired_product = "Sulmóvel"
    pages = [MOBILE_PLANS[index:index + 2] for index in range(0, len(MOBILE_PLANS), 2)]
    page = max(1, min(page, len(pages)))
    plans = pages[page - 1]
    if page == 1:
        decision.reply_text = (
            "No Sulmóvel temos planos nacionais com valores confirmados. "
            "Escolha uma opção para continuar:"
        )
    elif page == 2:
        decision.reply_text = "Mais opções do Sulmóvel:"
    else:
        decision.reply_text = "Últimas opções do Sulmóvel:"
    last = page == len(pages)
    decision.quick_replies = [
        QuickReply(
            id=f"mobile_{int(plan['data_gb'])}gb",
            label=f"{int(plan['data_gb'])}GB - R$ {_format_brl(float(plan['price']))}",
        )
        for plan in plans
    ] + [QuickReply(id="mobile_voltar", label="Voltar") if last else QuickReply(id="mobile_mais", label="Mais planos")]
    decision.next_action = "Cliente escolher " + " ou ".join(
        f"{int(plan['data_gb'])}GB" for plan in plans
    ) + (", ou voltar aos primeiros planos." if last else ", ou abrir mais planos móveis.")

    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.summary = "Cliente está consultando o catálogo Sulmóvel."
    _reset_catalog_handoff(decision)
    return decision

def _show_fiber_catalog(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    city = _city_from_sales_context(decision, context)
    decision.qualification.desired_product = "Fibra óptica"
    if not city:
        decision.reply_text = (
            "Claro! Para eu mostrar somente os planos de fibra óptica disponíveis, "
            "qual é a sua cidade?"
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.recommended_stage = "CONTATO_REALIZADO"
        decision.summary = "Cliente escolheu consultar os planos de fibra óptica."
        decision.next_action = "Confirmar a cidade para listar os planos de fibra válidos."
        _reset_catalog_handoff(decision)
        return decision

    eligible = _eligible_plans_for_city(city)
    if not eligible:
        return decision

    decision.qualification.city = city
    option_parts = [
        f"{int(plan['download_mbps'])} Mega por R$ {_format_brl(float(plan['price_fidelity']))}"
        for plan in eligible
    ]
    if len(option_parts) == 1:
        options_text = option_parts[0]
    else:
        options_text = ", ".join(option_parts[:-1]) + f" e {option_parts[-1]}"
    decision.reply_text = (
        f"Em {city}, os planos de fibra óptica disponíveis são {options_text} mensais. "
        "Qual você quer conhecer?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"plano_{int(plan['download_mbps'])}",
            label=f"{int(plan['download_mbps'])} Mega",
        )
        for plan in eligible[:3]
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.summary = f"Cliente está consultando os planos de fibra disponíveis em {city}."
    decision.next_action = "Cliente escolher um plano de fibra válido para avançar."
    decision.proposal_summary = (
        f"Planos de fibra válidos em {city}: " + "; ".join(option_parts) + "."
    )
    _reset_catalog_handoff(decision)
    return decision


def _selected_fiber_plan(value: str | None, city: str) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", folded)
    if not match:
        return None
    speed = int(match.group(1))
    return next(
        (
            plan
            for plan in _eligible_plans_for_city(city)
            if int(plan["download_mbps"]) == speed
        ),
        None,
    )


def _selected_mobile_plan(value: str | None) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(2|5|7|10|15)\s*gb\b", folded)
    if not match:
        return None
    data_gb = int(match.group(1))
    return next(
        (plan for plan in MOBILE_PLANS if int(plan["data_gb"]) == data_gb),
        None,
    )

def _show_selected_fiber_plan(
    decision: AIDecision,
    context: dict,
    plan: dict,
) -> AIDecision:
    city = _city_from_sales_context(decision, context)
    price = float(plan["price_fidelity"])
    speed = int(plan["download_mbps"])
    decision.reply_text = (
        f"Legal! Em {city} temos o plano de fibra óptica de {speed} Mega "
        f"por R$ {_format_brl(price)} mensais. Quer aproveitar e contratar esse plano agora?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_fibra", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.qualification.city = city
    decision.qualification.desired_product = f"Fibra {speed} Mega"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano de fibra de {speed} Mega em {city}."
    decision.next_action = "Confirmar se o cliente quer contratar o plano escolhido."
    decision.proposal_summary = (
        f"Fibra {speed} Mega em {city} por R$ {_format_brl(price)} mensais."
    )
    return decision


def _show_selected_mobile_plan(
    decision: AIDecision,
    plan: dict,
) -> AIDecision:
    data_gb = int(plan["data_gb"])
    price = float(plan["price"])
    apps = ", ".join(plan.get("unlimited_apps") or [])
    details = f" com {apps} ilimitado" if apps else ""
    if plan.get("minutes") and plan.get("sms"):
        extras = f", com {int(plan['minutes'])} minutos e {int(plan['sms'])} SMS{details}"
    else:
        extras = details
    decision.reply_text = (
        f"Legal! O Sulmóvel {data_gb}GB custa R$ {_format_brl(price)} mensais{extras}. "
        "Quer contratar esse plano?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_movel", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.qualification.desired_product = f"Sulmóvel {data_gb}GB"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano Sulmóvel {data_gb}GB."
    decision.next_action = "Confirmar se o cliente quer contratar o plano móvel escolhido."
    decision.proposal_summary = (
        f"Sulmóvel {data_gb}GB por R$ {_format_brl(price)} mensais."
    )
    return decision

def _looks_like_corporate_differential_question(value: str | None) -> bool:
    folded = _fold_sales_text(value)
    if not folded:
        return False
    cues = (
        "qual a diferenca",
        "qual diferenca",
        "diferencial",
        "vantagem",
        "porque e mais caro",
        "por que e mais caro",
        "pq e mais caro",
        "sla",
        "8 horas",
        "oito horas",
        "suporte nivel 2",
        "suporte n2",
        "nivel 2",
        "n2",
        "banda compartilhada",
        "compartilhada",
        "link dedicado",
        "ip fixo",
        "sulnet monitora",
        "monitora",
        "noc",
        "monitoramento",
        "queda",
        "oscilacao",
        "empresa ficar parada",
        "atendimento prioritario",
        "atendimento empresarial",
    )
    return any(cue in folded for cue in cues)


def _show_corporate_differentials(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "O link corporativo é pensado para manter sua empresa operando: tem SLA de atendimento de até 8 horas, "
        "suporte nível 2 e equipe especializada em empresas. A entrega da banda é diferenciada e não é compartilhada "
        "da mesma forma que no residencial; além disso, os planos têm IP fixo e Sulnet Monitora, com o NOC acompanhando "
        "quedas ou oscilações e atuando de forma proativa sempre que possível. Quer ver as opções corporativas?"
    )
    decision.quick_replies = [
        QuickReply(id="ver_planos_corp", label="Ver planos"),
        QuickReply(id="outra_duvida_corp", label="Outra dúvida"),
    ]
    decision.intent = "INFORMATION"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = "Internet corporativa"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.summary = (
        "Cliente recebeu os diferenciais do link corporativo: SLA de atendimento até 8h, suporte N2, "
        "equipe empresarial, banda diferenciada, IP fixo e monitoramento proativo do NOC."
    )
    decision.next_action = "Oferecer o catálogo corporativo após esclarecer os diferenciais."
    decision.proposal_summary = (
        "Diferenciais corporativos: SLA de atendimento até 8h; suporte N2; equipe especializada; "
        "entrega de banda diferenciada; IP fixo; Sulnet Monitora/NOC."
    )
    return decision


def _show_corporate_catalog(decision: AIDecision) -> AIDecision:
    option_parts = [
        f"{int(plan['download_mbps'])} Mega por R$ {_format_brl(float(plan['price']))}"
        for plan in CORPORATE_PLANS
    ]
    decision.reply_text = (
        "Perfeito. Para empresa temos planos corporativos de fibra óptica com atendimento prioritário: "
        + ", ".join(option_parts[:-1])
        + f" e {option_parts[-1]} mensais. Qual você quer conhecer?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"corp_{int(plan['download_mbps'])}",
            label=f"Corp. {int(plan['download_mbps'])} Mega",
        )
        for plan in CORPORATE_PLANS
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = "Internet corporativa"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.summary = "Cliente confirmou atendimento para empresa e recebeu o catálogo corporativo."
    decision.next_action = "Cliente escolher entre Corp. 200, Corp. 250 e Corp. 650."
    decision.proposal_summary = "Planos corporativos: " + "; ".join(option_parts) + "."
    return decision


def _selected_corporate_plan(value: str | None) -> dict | None:
    folded = _fold_sales_text(value)
    match = re.search(r"\b(200|250|650)\s*(?:mega|mb|mbps)?\b", folded)
    if not match:
        return None
    speed = int(match.group(1))
    return next(
        (
            plan
            for plan in CORPORATE_PLANS
            if int(plan["download_mbps"]) == speed
        ),
        None,
    )


def _show_selected_corporate_plan(
    decision: AIDecision,
    plan: dict,
) -> AIDecision:
    speed = int(plan["download_mbps"])
    upload = int(plan["upload_mbps"])
    price = float(plan["price"])
    benefits = ", ".join(plan.get("benefits") or [])
    decision.reply_text = (
        f"O Corp. {speed} Mega custa R$ {_format_brl(price)} mensais, com {speed}/{upload} Mbps, "
        f"{benefits} e atendimento prioritário. Quer contratar esse plano?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="ver_outros_corp", label="Ver outros planos"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.qualification.customer_type = "EMPRESA"
    decision.qualification.desired_product = f"Corporativo {speed} Mega"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = price
    decision.concession_level = 0
    decision.free_months = 0
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.summary = f"Cliente escolheu o plano corporativo de {speed} Mega."
    decision.next_action = "Confirmar se o cliente quer contratar o plano corporativo escolhido."
    decision.proposal_summary = (
        f"Corp. {speed} Mega por R$ {_format_brl(price)} mensais; {speed}/{upload} Mbps; {benefits}."
    )
    return decision


def _apply_corporate_customer_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    customer_type = _customer_type_from_context(context)
    if customer_type == "RESIDENCIAL":
        decision.qualification.customer_type = "RESIDENCIAL"
        return decision
    if customer_type != "EMPRESA":
        return decision

    decision.qualification.customer_type = "EMPRESA"
    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _fold_sales_text(_last_outbound_text(context))

    if _looks_like_corporate_differential_question(latest_raw):
        return _show_corporate_differentials(decision)

    if latest in {"ver planos", "ver planos corporativos", "planos corporativos"}:
        return _show_corporate_catalog(decision)

    if _customer_type_from_text(latest_raw) == "EMPRESA":
        return _show_corporate_catalog(decision)

    if _looks_like_catalog_request(latest_raw):
        return _show_corporate_catalog(decision)

    if latest == "ver outros planos" and (
        "corp " in previous
        or "corporativo" in previous
        or "empresa temos planos" in previous
    ):
        return _show_corporate_catalog(decision)

    selected = _selected_corporate_plan(latest_raw)
    if selected and (
        "corp" in latest
        or "corporativo" in previous
        or "empresa temos planos" in previous
        or str((context.get("lead") or {}).get("confirmed_customer_type") or "").upper() == "EMPRESA"
    ):
        return _show_selected_corporate_plan(decision, selected)

    offer_code = str(decision.offer_plan_code or "").upper()
    residential_offer = offer_code.startswith("FTTH_")
    residential_reply = any(
        cue in _fold_sales_text(decision.reply_text)
        for cue in (
            "800 mega por r 119 90",
            "650 mega por r 99 90",
            "250 mega por r 89 90",
            "500 mega por r 119 90",
        )
    )
    if residential_offer or residential_reply:
        return _show_corporate_catalog(decision)

    concession_in_reply = any(
        cue in _fold_sales_text(decision.reply_text)
        for cue in (
            "mes gratis",
            "meses gratis",
            "mensalidade gratis",
            "mensalidades gratis",
        )
    )
    if decision.free_months > 0 or decision.concession_level > 0 or concession_in_reply:
        decision = _show_corporate_catalog(decision)
        decision.reply_text = (
            "Para os planos corporativos, não tenho desconto ou mensalidade grátis confirmados na base vigente. "
            "Posso te apresentar as opções oficiais: 200 Mega por R$ 139,00, 250 Mega por R$ 169,00 "
            "ou 650 Mega por R$ 199,90 mensais. Qual você quer conhecer?"
        )
        return decision

    decision.concession_level = 0
    decision.free_months = 0
    return decision


def _reset_entry_offer(decision: AIDecision) -> None:
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True


def _existing_customer_plan_change_requested(context: dict) -> bool:
    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    recent_inbound = [
        _fold_sales_text(str(item.get("body") or ""))
        for item in (context.get("history") or [])[-12:]
        if str(item.get("direction") or "").upper() == "INBOUND"
    ]
    combined = " | ".join([*recent_inbound, latest])

    change_cues = (
        "aumentar",
        "aumentar meu plano",
        "aumentar o plano",
        "mais velocidade",
        "mais mega",
        "subir meu plano",
        "subir o plano",
        "trocar meu plano",
        "trocar o plano",
        "mudar meu plano",
        "mudar o plano",
        "alterar meu plano",
        "alterar o plano",
        "upgrade",
        "melhorar meu plano",
    )
    existing_service_cues = (
        "ja tenho um plano",
        "tenho um plano",
        "ja tenho plano",
        "meu plano",
        "sou cliente sulnet",
        "ja sou cliente sulnet",
        "tenho internet da sulnet",
        "ja tenho internet da sulnet",
        "minha internet sulnet",
        "minha conexao sulnet",
    )

    wants_change = any(cue in latest for cue in change_cues)
    if not wants_change:
        return False

    confirmed_existing = _existing_customer_from_context(context) == "CLIENTE"
    mentions_existing_service = any(cue in combined for cue in existing_service_cues)
    return confirmed_existing or mentions_existing_service


# Assuntos de quem já é cliente que não são venda nova: vão para a central.
_EXISTING_CUSTOMER_SERVICE_CUES = (
    "upgrade ou suporte",
    "upgrade",
    "suporte tecnico",
    "preciso de suporte",
    "falar com o suporte",
    "falar com suporte",
    "financeiro",
    "2 via",
    "segunda via",
    "boleto da minha",
    "minha fatura",
    "sem internet",
    "internet caiu",
    "internet lenta",
    "internet ta lenta",
    "internet esta lenta",
    "internet nao funciona",
    "internet parou",
    "mudanca de endereco",
    "cancelar meu plano",
    "cancelar minha internet",
)


def _existing_customer_service_requested(context: dict) -> bool:
    if _existing_customer_from_context(context) != "CLIENTE":
        return False
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    return bool(latest) and any(cue in latest for cue in _EXISTING_CUSTOMER_SERVICE_CUES)


def _apply_existing_customer_plan_change_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if not (
        _existing_customer_plan_change_requested(context)
        or _existing_customer_service_requested(context)
    ):
        return decision

    # Canal exclusivo comercial: upgrade, suporte e financeiro vão para a central,
    # com botão que abre o WhatsApp oficial (o envio monta o botão de link).
    decision.reply_text = CENTRAL_REDIRECT_MESSAGE
    decision.quick_replies = []
    decision.intent = "INFORMATION"
    decision.sales_state = "WAITING_CUSTOMER"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "CLIENTE"
    decision.handoff_target = "NONE"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.summary = (
        "Cliente atual pediu upgrade, suporte ou financeiro e foi direcionado à central "
        f"{PLAN_CHANGE_SERVICE_PHONE}."
    )
    decision.next_action = "Tratativa comercial encerrada; o assunto segue pela central de atendimento."
    decision.journey_state = "DISCOVERY"
    decision.journey_pending_action = "Nenhuma ação comercial pendente."
    decision.service_redirect = True
    return decision


def _show_existing_customer_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = "Antes de te mostrar as opções, você já é cliente Sulnet?"
    decision.quick_replies = [
        QuickReply(id="cliente_sulnet_sim", label="Sim"),
        QuickReply(id="cliente_sulnet_nao", label="Não"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "NAO_INFORMADO"
    decision.summary = "SulIA iniciou a qualificação perguntando se a pessoa já é cliente Sulnet."
    decision.next_action = "Confirmar se já é cliente Sulnet."
    _reset_entry_offer(decision)
    return decision


def _show_new_customer_product_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Perfeito! O que você quer conhecer: planos de internet ou Telefonia Móvel?"
    )
    decision.quick_replies = [
        QuickReply(id="prospeccao_internet", label="Internet"),
        QuickReply(id="prospeccao_movel", label="Telefonia Móvel"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "NAO_CLIENTE"
    decision.qualification.desired_product = None
    decision.summary = "Pessoa confirmou que ainda não é cliente Sulnet."
    decision.next_action = "Cliente escolher entre Internet e Telefonia Móvel."
    _reset_entry_offer(decision)
    return decision


def _show_existing_customer_product_question(decision: AIDecision) -> AIDecision:
    decision.reply_text = (
        "Tudo bem! Como posso te ajudar hoje? Por aqui eu faço novas contratações: "
        "Telefonia Móvel ou uma nova conexão de internet para outro endereço. "
        "Se for upgrade, suporte ou financeiro, toque em \"Upgrade ou suporte\"."
    )
    decision.quick_replies = [
        QuickReply(id="cliente_movel", label="Telefonia Móvel"),
        QuickReply(id="cliente_nova_conexao", label="Nova conexão"),
        QuickReply(id="cliente_upgrade_suporte", label="Upgrade ou suporte"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = "CLIENTE"
    decision.qualification.desired_product = None
    decision.summary = "Pessoa confirmou que já é cliente Sulnet e busca uma nova contratação."
    decision.next_action = "Cliente escolher Telefonia Móvel ou nova conexão para outro endereço."
    _reset_entry_offer(decision)
    return decision


def _show_internet_segment_question(
    decision: AIDecision,
    *,
    existing_customer: bool,
) -> AIDecision:
    decision.reply_text = (
        "Essa nova conexão para outro endereço é para sua casa ou empresa?"
        if existing_customer
        else "A internet é para sua casa ou empresa?"
    )
    decision.quick_replies = [
        QuickReply(id="internet_casa", label="Para casa"),
        QuickReply(id="internet_empresa", label="Para empresa"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = (
        "CLIENTE" if existing_customer else "NAO_CLIENTE"
    )
    decision.qualification.desired_product = (
        "Nova conexão de internet" if existing_customer else "Internet"
    )
    decision.summary = (
        "Cliente Sulnet escolheu contratar uma nova conexão para outro endereço."
        if existing_customer
        else "Novo cliente escolheu conhecer planos de internet."
    )
    decision.next_action = "Confirmar se a conexão é residencial ou empresarial."
    _reset_entry_offer(decision)
    return decision


def _show_black_offer_question(decision: AIDecision, status: str) -> AIDecision:
    if status == "CLIENTE":
        decision.reply_text = black.CUSTOMER_OFFER_QUESTION
        decision.quick_replies = [
            QuickReply(id="black_sim", label="Quero saber"),
            QuickReply(id="black_nao", label="Agora não"),
        ]
    else:
        decision.reply_text = black.OFFER_QUESTION
        decision.quick_replies = [
            QuickReply(id="black_sim", label="Sim"),
            QuickReply(id="black_nao", label="Não"),
        ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = status
    decision.qualification.desired_product = None
    decision.summary = "SulIA perguntou se a pessoa quer conhecer a promoção Black da Sulnet."
    decision.next_action = "Cliente responder se quer conhecer a Black."
    _reset_entry_offer(decision)
    return decision


def _show_black_options(decision: AIDecision, status: str) -> AIDecision:
    decision.reply_text = black.OPTIONS_TEXT
    decision.quick_replies = [
        QuickReply(id="black_movel", label="Só móvel"),
        QuickReply(id="black_combo", label="Combo fibra + móvel"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.recommended_stage = "CONTATO_REALIZADO"
    decision.qualification.existing_customer = status
    decision.qualification.desired_product = None
    decision.summary = "Pessoa quer conhecer a Black; SulIA apresentou móvel ou combo."
    decision.next_action = "Cliente escolher entre móvel e combo da Black."
    _reset_entry_offer(decision)
    return decision


_BLACK_VALIDITY_CUES = ("ate quando", "validade", "vale ate", "ate que dia", "prazo da promocao", "termina quando")


def _answer_black_validity(decision: AIDecision, status: str) -> AIDecision:
    decision.reply_text = (
        f"A Black da Sulnet vale para contratações feitas até {black.VALID_UNTIL_LABEL}, "
        f"com a 1ª mensalidade só em {black.FIRST_DUE_DATE}. Qual opção você prefere?"
    )
    decision.quick_replies = [
        QuickReply(id="black_movel", label="Só móvel"),
        QuickReply(id="black_combo", label="Combo fibra + móvel"),
    ]
    decision.intent = "INFORMATION"
    decision.sales_state = "DISCOVERY"
    decision.qualification.existing_customer = status
    decision.summary = "Cliente perguntou a validade da Black; SulIA respondeu e voltou às opções."
    decision.next_action = "Cliente escolher entre móvel e combo da Black."
    _reset_entry_offer(decision)
    return decision


def _apply_black_entry(decision: AIDecision, context: dict, status: str, *, just_identified: bool) -> AIDecision | None:
    """Etapa da Black logo após "já é cliente?". None = segue o fluxo normal."""
    if not black.black_active():
        return None
    previous = _last_outbound_text(context)
    latest_raw = str(context.get("latest_customer_message") or "")
    if (black.is_offer_question(previous) or black.is_options_question(previous)) and any(
        cue in _fold_sales_text(latest_raw) for cue in _BLACK_VALIDITY_CUES
    ):
        return _answer_black_validity(decision, status)
    if just_identified and not black.was_offered(context.get("history")):
        return _show_black_offer_question(decision, status)
    if black.is_offer_question(previous):
        answer = black.answer_to_offer(latest_raw)
        if answer == "YES":
            return _show_black_options(decision, status)
        if answer == "NO":
            if status == "CLIENTE":
                return _show_existing_customer_product_question(decision)
            return _show_new_customer_product_question(decision)
        # Pergunta livre ("até quando vai?"): o modelo responde com a base da Black.
        # Repetir a mesma pergunta travava a conversa (integridade: REPEATED_QUESTION).
        return None
    if black.is_options_question(previous):
        choice = black.choice_from_options(latest_raw)
        if choice == "MOBILE":
            result = _show_mobile_catalog_page(decision, page=1)
            result.qualification.existing_customer = status
            return result
        if choice == "COMBO":
            return _show_internet_segment_question(decision, existing_customer=status == "CLIENTE")
        return None
    return None


def _apply_prospecting_entry_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if decision.intent in {"DO_NOT_CONTACT", "HUMAN_REQUEST"} or decision.sales_state == "OPT_OUT":
        return decision

    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _last_outbound_text(context)
    current_confirmation = _existing_customer_from_text(
        latest_raw,
        previous_outbound=previous,
    )
    status = _existing_customer_from_context(context)

    if not status:
        return _show_existing_customer_question(decision)

    decision.qualification.existing_customer = status

    black_result = _apply_black_entry(
        decision, context, status, just_identified=current_confirmation in {"CLIENTE", "NAO_CLIENTE"}
    )
    if black_result is not None:
        return black_result

    if current_confirmation == "CLIENTE":
        return _show_existing_customer_product_question(decision)
    if current_confirmation == "NAO_CLIENTE":
        return _show_new_customer_product_question(decision)

    mobile_choices = {
        "movel",
        "telefonia movel",
        "plano movel",
        "planos moveis",
        "sulmovel",
        "celular",
    }
    internet_choices = {
        "internet",
        "planos de internet",
        "plano de internet",
        "fibra",
        "fibra optica",
    }
    new_connection_choices = {
        "nova conexao",
        "outra conexao",
        "nova internet",
        "novo endereco",
        "outro endereco",
        "internet para outro endereco",
    }

    if status == "CLIENTE":
        if latest in mobile_choices:
            result = _show_mobile_catalog_page(decision, page=1)
            result.qualification.existing_customer = "CLIENTE"
            return result
        if latest in new_connection_choices or latest in internet_choices:
            return _show_internet_segment_question(
                decision,
                existing_customer=True,
            )
        return decision

    if latest in mobile_choices:
        result = _show_mobile_catalog_page(decision, page=1)
        result.qualification.existing_customer = "NAO_CLIENTE"
        return result

    if latest in internet_choices:
        customer_type = _customer_type_from_context(context)
        if customer_type == "EMPRESA":
            result = _show_corporate_catalog(decision)
            result.qualification.existing_customer = "NAO_CLIENTE"
            return result
        if customer_type == "RESIDENCIAL":
            result = _show_fiber_catalog(decision, context)
            result.qualification.existing_customer = "NAO_CLIENTE"
            return result
        return _show_internet_segment_question(
            decision,
            existing_customer=False,
        )

    return decision


def _apply_catalog_navigation_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    latest_raw = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_raw)
    previous = _fold_sales_text(_last_outbound_text(context))

    if _looks_like_catalog_request(latest_raw):
        return _show_catalog_root(decision)

    if latest in {
        "fibra",
        "fibra optica",
        "planos de fibra",
        "internet fibra",
        "internet de fibra",
    }:
        return _show_fiber_catalog(decision, context)

    if latest in {
        "movel",
        "sulmovel",
        "planos moveis",
        "plano movel",
        "celular",
        "so movel",
        "so o movel",
        "somente movel",
        "apenas movel",
        "telefonia movel",
    } or "so movel" in latest:
        return _show_mobile_catalog_page(decision, page=1)

    # "Quero conhecer opções" estoura o limite de 20 caracteres do botão
    # ("Quero conhecer opçõe") e o cliente pede a lista depois de um plano único.
    wants_plan_list = any(
        cue in latest
        for cue in (
            "quero conhecer",
            "conhecer opco",
            "ver opco",
            "outras opco",
            "ver planos",
            "ver os planos",
            "quais planos",
        )
    )
    previous_is_mobile = any(
        token in previous for token in ("sulmovel", "so movel", "telefonia movel", " gb", "portabilidade")
    )
    if wants_plan_list and previous_is_mobile:
        return _show_mobile_catalog_page(decision, page=1)

    if latest in {"mais planos", "ver mais", "mais opcoes"}:
        return _show_mobile_catalog_page(
            decision,
            page=3 if "mais opcoes do sulmovel" in previous else 2,
        )

    if latest == "voltar" and "sulmovel" in previous:
        return _show_mobile_catalog_page(decision, page=1)

    if latest == "ver outros planos":
        if "sulmovel" in previous:
            return _show_mobile_catalog_page(decision, page=1)
        if "fibra optica" in previous:
            return _show_fiber_catalog(decision, context)

    if (
        "qual e a sua cidade" in previous
        and "planos de fibra" in previous
        and _city_from_sales_context(decision, context)
    ):
        return _show_fiber_catalog(decision, context)

    mobile_plan = _selected_mobile_plan(latest_raw)
    if mobile_plan:
        mobile_gb = int(mobile_plan["data_gb"])
        mobile_compact = latest in {f"{mobile_gb}gb", f"{mobile_gb} gb"}
        mobile_button_label = (
            latest.startswith(f"{mobile_gb}gb r ")
            or latest.startswith(f"{mobile_gb} gb r ")
        )
        if "sulmovel" in previous or mobile_compact or mobile_button_label:
            return _show_selected_mobile_plan(decision, mobile_plan)

    city = _city_from_sales_context(decision, context)
    if city:
        fiber_plan = _selected_fiber_plan(latest_raw, city)
        if fiber_plan and (
            "planos de fibra" in previous
            or "fibra optica" in previous
            or latest in {
                f"{int(fiber_plan['download_mbps'])} mega",
                f"{int(fiber_plan['download_mbps'])} mb",
                f"{int(fiber_plan['download_mbps'])} mbps",
            }
        ):
            return _show_selected_fiber_plan(decision, context, fiber_plan)

    return decision


def _apply_unavailable_plan_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    requested_speed = _requested_plan_speed(
        str(context.get("latest_customer_message") or "")
    )
    if requested_speed is None:
        return decision

    city = _city_from_sales_context(decision, context)
    if not city:
        decision.reply_text = (
            f"A disponibilidade do plano de {requested_speed} Mega varia por cidade. "
            "Qual é a sua cidade? Aí te passo as opções corretas e seguimos."
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.customer_commitment = False
        decision.offer_plan_code = None
        decision.offer_price = None
        decision.followup_needed = True
        decision.next_action = "Confirmar a cidade para apresentar somente planos válidos."
        return decision

    eligible = _eligible_plans_for_city(city)
    if not eligible:
        return decision

    if any(
        int(plan["download_mbps"]) == requested_speed
        for plan in eligible
    ):
        return decision

    option_parts = [
        f"{plan['download_mbps']} Mega por R$ {_format_brl(float(plan['price_fidelity']))}"
        for plan in eligible
    ]
    if len(option_parts) == 1:
        options_text = option_parts[0]
    else:
        options_text = ", ".join(option_parts[:-1]) + f" ou {option_parts[-1]}"

    decision.reply_text = (
        f"O plano de {requested_speed} Mega não está disponível em {city}. "
        f"Hoje temos {options_text} mensais. Qual opção você prefere?"
    )
    decision.quick_replies = [
        QuickReply(
            id=f"plano_{int(plan['download_mbps'])}",
            label=f"{int(plan['download_mbps'])} Mega",
        )
        for plan in eligible[:3]
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.offer_plan_code = None
    decision.offer_price = None
    decision.concession_level = 0
    decision.free_months = 0
    decision.followup_needed = True
    decision.next_action = "Cliente escolher uma das opções comerciais válidas para a cidade."
    decision.proposal_summary = (
        f"Alternativas válidas em {city}: " + "; ".join(option_parts) + "."
    )
    return decision


def _apply_router_information_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    router_cues = (
        "roteador",
        "roteadores",
        "wifi",
        "wi fi",
        "segundo roteador",
        "outro roteador",
        "mais um roteador",
    )
    if not any(cue in latest for cue in router_cues):
        return decision

    explicit_speed = re.search(r"\b(\d{2,4})\s*(?:mega|mb|mbps)\b", latest)
    if explicit_speed and int(explicit_speed.group(1)) not in {650, 800}:
        return decision

    plan = _plan_from_sales_context(decision, context)
    if not plan:
        speed_match = re.search(r"\b(650|800)\s*mega\b", latest)
        if speed_match:
            speed = int(speed_match.group(1))
            plan = next(
                (item for item in FTTH_PLANS if int(item["download_mbps"]) == speed),
                None,
            )
    if not plan:
        return decision

    speed = int(plan["download_mbps"])
    if speed not in {650, 800}:
        return decision

    included = int(plan.get("included_router_count") or 1)
    additional_price = float(plan.get("additional_router_monthly_price") or 29.90)
    plan_price = float(plan["price_fidelity"])
    asks_fiber = any(
        cue in latest
        for cue in (
            "e fibra",
            "é fibra",
            "internet e fibra",
            "internet é fibra",
            "fibra optica",
            "fibra óptica",
        )
    )

    if asks_fiber:
        opening = (
            f"Sim, o plano de {speed} Mega é de fibra óptica e inclui "
            f"{included} roteador em comodato."
        )
    else:
        opening = (
            f"O plano de {speed} Mega inclui {included} roteador em comodato."
        )

    decision.reply_text = (
        f"{opening} Se precisar de outro, cada roteador adicional é locado por "
        f"R$ {_format_brl(additional_price)} por mês. "
        f"Quer seguir com o plano de {speed} Mega por R$ {_format_brl(plan_price)} mensais?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="tenho_duvida", label="Tenho outra dúvida"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.recommended_stage = "PROPOSTA"
    decision.offer_plan_code = str(plan["code"])
    decision.offer_price = plan_price
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = False
    decision.followup_needed = True
    decision.objection = "NONE"
    decision.unanswered_question = None
    decision.summary = (
        f"Cliente perguntou sobre roteadores do plano de {speed} Mega; "
        f"informado {included} incluso e adicional por R$ {_format_brl(additional_price)}/mês."
    )
    decision.next_action = "Retomar o fechamento do plano após esclarecer os roteadores."
    decision.proposal_summary = (
        f"Fibra {speed} Mega por R$ {_format_brl(plan_price)} mensais; "
        f"{included} roteador incluso; adicional por R$ {_format_brl(additional_price)}/mês."
    )
    return decision


def _apply_secondary_decline_reengagement_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if _customer_type_from_context(context) == "EMPRESA":
        return decision

    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if latest not in _SECONDARY_DECLINES:
        return decision

    previous_outbound = None
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            previous_outbound = str(row.get("body") or "")
            break
    if not previous_outbound:
        return decision

    previous_folded = _fold_sales_text(previous_outbound)
    # Se a pergunta anterior era diretamente sobre contratar/fechar, a recusa
    # pode ser da oferta principal e não deve ser reinterpretada.
    if "contratar" in previous_folded or "fechar" in previous_folded:
        return decision

    is_auxiliary = any(cue in previous_folded for cue in _AUXILIARY_QUESTION_CUES)
    if not is_auxiliary:
        return decision

    plan = _plan_from_sales_context(decision, context)
    if not plan:
        return decision

    price = float(plan["price_fidelity"])
    price_text = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    decision.reply_text = (
        "Sem problema! Podemos dar andamento à contratação do plano de fibra óptica de "
        f"{plan['download_mbps']} Mega por R$ {price_text} mensais?"
    )
    decision.quick_replies = [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id="outra_duvida", label="Tenho outra dúvida"),
    ]
    decision.intent = "INTEREST"
    decision.sales_state = "OFFERING"
    decision.objection = "NONE"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.followup_needed = True
    decision.next_action = "Retomar o fechamento do plano já apresentado."
    return decision


_OFFER_REJECTION_CUES = (
    "nao quero nada de internet",
    "nao quero internet",
    "nao tenho interesse em internet",
    "nao quero esse plano",
    "nao quero o plano",
    "nao quero contratar",
    "nao tenho interesse",
    "nao quero nada",
)

_EXPLICIT_CONTACT_STOP_CUES = (
    "nao quero mais mensagens",
    "nao quero mais contato",
    "nao me contate",
    "nao entre em contato",
    "nao me chame",
    "pare de me chamar",
    "pare de mandar",
    "me remova",
    "remover meu numero",
    "sair da lista",
    "descadastrar",
)


def _is_company_context(context: dict) -> bool:
    if _customer_type_from_context(context) == "EMPRESA":
        return True
    company_terms = (
        "minha empresa",
        "nossa empresa",
        "para empresa",
        "empresa",
        "cnpj",
        "meu negocio",
        "meu comercio",
        "minha loja",
        "meu escritorio",
    )
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if any(term in body for term in company_terms):
            return True
    return False


def _is_residential_context(context: dict) -> bool:
    if _customer_type_from_context(context) == "RESIDENCIAL":
        return True
    residential_terms = (
        "minha casa",
        "para casa",
        "em casa",
        "residencial",
        "minha residencia",
        "meu apartamento",
    )
    for row in reversed(context.get("history") or []):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if any(term in body for term in residential_terms):
            return True
    return False


def _apply_offer_rejection_diagnostic_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if not latest:
        return decision
    if any(cue in latest for cue in _EXPLICIT_CONTACT_STOP_CUES):
        return decision
    if not any(cue in latest for cue in _OFFER_REJECTION_CUES):
        return decision

    # Não repetir esse mesmo gatilho indefinidamente.
    for row in context.get("history") or []:
        if str(row.get("direction") or "").upper() != "OUTBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if (
            "possui conexao de internet na sua casa" in body
            or "sua empresa ja possui conexao de internet" in body
        ):
            return decision

    if _is_company_context(context):
        question = "Entendi. Hoje sua empresa já possui conexão de internet?"
    elif _is_residential_context(context):
        question = "Entendi. Hoje você já possui conexão de internet na sua casa?"
    else:
        question = "Entendi. Hoje você já possui conexão de internet?"

    decision.reply_text = question
    decision.quick_replies = [
        QuickReply(id="possui_internet_sim", label="Sim"),
        QuickReply(id="possui_internet_nao", label="Não"),
    ]
    decision.intent = "QUALIFYING"
    decision.sales_state = "DISCOVERY"
    decision.objection = "NO_INTEREST"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.followup_needed = True
    decision.next_action = (
        "Descobrir a situação atual da conexão antes de retomar uma oferta."
    )
    return decision


_FIRM_REJECTION_CUES = _OFFER_REJECTION_CUES + _EXPLICIT_CONTACT_STOP_CUES + (
    "nao preciso mais",
    "nao quero mais",
    "nao vou querer",
    "sem interesse",
    "nao temos interesse",
    "nao tenho interesse",
    "nao tem interesse",
    "desisti",
    "desisto",
)
_FIRM_REJECTION_EXACT = {"nao obrigado", "nao obrigada", "dispenso", "prefiro nao", "nao quero"}

REJECTION_CLOSE_MESSAGE = (
    "Tudo bem, respeito a sua decisão e não vou mais insistir. "
    "Se um dia precisar da Sulnet, é só chamar por aqui. Obrigado pela atenção!"
)


def _is_firm_rejection(text: str | None) -> bool:
    folded = _fold_sales_text(text)
    return bool(folded) and (
        folded in _FIRM_REJECTION_EXACT or any(cue in folded for cue in _FIRM_REJECTION_CUES)
    )


_BARE_NEGATIVE_ANSWERS = {"nao", "n", "agora nao", "nao valeu"}
_INTEREST_CUES = (
    "quero",
    "interesse",
    "plano",
    "preco",
    "valor",
    "contratar",
    "como funciona",
    "quanto",
    "mega",
    "movel",
    "internet",
)
_OFFER_CUES = ("quer conhecer", "promocao", "black", "tem interesse", "quer contratar", "gostaria de conhecer")


def _apply_repeated_rejection_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    """Cliente sem interesse: a IA tenta uma vez; se ele recusar de novo, encerra.

    Pedido do comercial (05/11): respeitar o cliente depois de uma tentativa.
    Um "não" seco a uma oferta conta como recusa (conversa 651: "Não obrigada",
    depois "Não" para a Black).
    """
    latest = context.get("latest_customer_message")
    folded_latest = _fold_sales_text(latest)
    offer_before = _fold_sales_text(_last_outbound_text(context))
    # "Agora não" / "Não" seco conta como recusa em qualquer pergunta nossa
    # (conversa Dona Ella, 06/11: "Agora não" respondido a uma pergunta de contrato).
    soft_decline = folded_latest in _BARE_NEGATIVE_ANSWERS
    if not (_is_firm_rejection(latest) or soft_decline):
        return decision
    rows = list(context.get("history") or [])
    # A mensagem atual (e a rajada dela) fica de fora: procuramos uma recusa anterior
    # seguida de uma tentativa nossa.
    while rows and str(rows[-1].get("direction") or "").upper() == "INBOUND":
        rows.pop()
    rejected_before = False
    attempted_after = False
    for row in rows:
        direction = str(row.get("direction") or "").upper()
        if direction == "INBOUND":
            body = _fold_sales_text(row.get("body"))
            # "Não" seco a uma pergunta ou oferta também é recusa (10/11: "Não" para
            # "já possui serviço?" e depois "Não" para a Black seguiu a venda).
            if _is_firm_rejection(row.get("body")) or body in _BARE_NEGATIVE_ANSWERS:
                rejected_before, attempted_after = True, False
            elif any(cue in body for cue in _INTEREST_CUES):
                # Voltou a se interessar: a recusa antiga não vale mais.
                rejected_before, attempted_after = False, False
            # "Sim" para "já é cliente?" e respostas curtas não mudam nada.
        elif direction == "OUTBOUND" and rejected_before:
            attempted_after = True
    if not (rejected_before and attempted_after):
        return decision

    decision.reply_text = REJECTION_CLOSE_MESSAGE
    decision.quick_replies = []
    decision.intent = "NOT_INTERESTED"
    decision.objection = "NO_INTEREST"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.followup_needed = False
    decision.next_action = "Cliente recusou duas vezes: não insistir nem mandar follow-up."
    return decision


def _recent_outbound_messages_for_repetition(
    context: dict,
    *,
    limit: int = 6,
) -> list[str]:
    rows = [
        str(item.get("body") or "").strip()
        for item in context.get("history", [])
        if str(item.get("direction") or "").upper() == "OUTBOUND"
        and str(item.get("body") or "").strip()
    ]
    return rows[-limit:]


_DIRECT_QUESTION_START = re.compile(
    r"^(?:e\s+)?(?:qual|quais|quanto|quantos|quantas|quando|como|onde|por que|porque|pq|"
    r"tem|posso|preciso|e se|o que|oque|existe|da pra|aceita|inclui|funciona|vem|vou pagar)\b"
)


def _customer_asked_direct_question(context: dict) -> bool:
    raw = str(context.get("latest_customer_message") or "")
    return "?" in raw or bool(_DIRECT_QUESTION_START.match(_fold_sales_text(raw)))


def _sdr_reply_nearly_identical(decision: AIDecision, context: dict) -> bool:
    candidate = _fold_sales_text(decision.reply_text)
    if not candidate:
        return False
    return any(
        candidate == _fold_sales_text(previous)
        or SequenceMatcher(None, candidate, _fold_sales_text(previous)).ratio() >= 0.92
        for previous in _recent_outbound_messages_for_repetition(context)
    )


def _sdr_reply_is_stale_repetition(decision: AIDecision, context: dict) -> bool:
    """Repetição que justifica trocar a resposta.

    Se o cliente fez uma pergunta direta, uma resposta que reaproveita termos da
    mensagem anterior (ex.: multa) é resposta, não estagnação: só conta se for
    praticamente igual. Antes, a regra trocava a resposta por "por outro ângulo"
    e o cliente ficava sem o valor da multa (simulação 03/11).
    """
    if not _sdr_reply_repeats_recent_outbound(decision, context):
        return False
    if _customer_asked_direct_question(context):
        return _sdr_reply_nearly_identical(decision, context)
    return True


def _sdr_reply_repeats_recent_outbound(decision: AIDecision, context: dict) -> bool:
    candidate = _fold_sales_text(decision.reply_text)
    if not candidate:
        return False
    candidate_tokens = set(candidate.split())
    for previous in _recent_outbound_messages_for_repetition(context):
        other = _fold_sales_text(previous)
        if not other:
            continue
        if candidate == other:
            return True
        if SequenceMatcher(None, candidate, other).ratio() >= 0.80:
            return True
        other_tokens = set(other.split())
        if candidate_tokens and other_tokens:
            containment = len(candidate_tokens & other_tokens) / min(
                len(candidate_tokens),
                len(other_tokens),
            )
            if containment >= 0.76:
                return True
    return False


_JOURNEY_PENDING_ACTIONS = {
    "IDENTIFY_CUSTOMER": "Confirmar se a pessoa já é cliente Sulnet.",
    "CHOOSE_PRODUCT": "Confirmar se deseja Internet, Telefonia Móvel ou nova conexão, conforme o perfil.",
    "CHOOSE_SEGMENT": "Confirmar se a conexão de internet é para casa ou empresa.",
    "CONFIRM_CITY": "Confirmar a cidade para validar os planos de internet disponíveis.",
    "SELECT_OFFER": "Apresentar opções válidas e obter a escolha de um plano.",
    "HANDLE_OBJECTION": "Resolver a objeção atual com um ângulo novo e avançar a decisão.",
    "CONFIRM_PURCHASE": "Obter aceite explícito para contratar a oferta escolhida.",
    "CLOSING": "Iniciar o Vendedor IA de Fechamento com todo o contexto coletado.",
    "HUMAN_HANDOFF": "Seguir atendendo pela IA: não existe atendimento humano.",
    "OPT_OUT": "Encerrar contato e cancelar follow-ups.",
    "DISCOVERY": "Descobrir a próxima informação comercial necessária sem repetir perguntas.",
}


def _journey_memory_from_context(context: dict) -> dict:
    value = (context.get("lead") or {}).get("journey")
    return dict(value) if isinstance(value, dict) else {}


def _explicit_human_request(context: dict) -> bool:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    cues = (
        "quero falar com atendente",
        "quero falar com um atendente",
        "falar com atendente",
        "falar com uma pessoa",
        "falar com pessoa",
        "quero falar com uma pessoa",
        "quero falar com humano",
        "atendimento humano",
        "falar com vendedor",
        "quero um vendedor",
        "quero falar com vendedor",
    )
    return any(cue in latest for cue in cues)


def _explicit_purchase_commitment(context: dict) -> bool:
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if any(
        cue in latest
        for cue in (
            "nao quero contratar",
            "nao quero assinar",
            "nao vou contratar",
            "nao vou assinar",
            "nao tenho interesse em assinar",
            "sem interesse em contratar",
        )
    ):
        return False

    cues = (
        "quero contratar",
        "quero assinar",
        "tenho interesse em assinar",
        "interesse em assinar",
        "gostaria de assinar",
        "quero aderir",
        "quero fechar",
        "pode fechar",
        "pode fazer",
        "vamos fechar",
        "vamos fazer",
        "vamos assinar",
        "quero esse plano",
        "quero esse pacote",
        "vou contratar",
        "vou assinar",
        "pode contratar",
        "pode assinar",
        "com essa condicao eu fecho",
        "fechado",
        "pode dar andamento",
        "vamos dar andamento",
        "pode seguir",
        "pode prosseguir",
        "eu aceito",
        "aceito",
    )
    if any(cue in latest for cue in cues):
        return True

    if re.search(
        r"\b(?:quero|gostaria|pretendo|vou|tenho interesse em)\s+"
        r"(?:contratar|assinar|fechar|aderir)\b",
        latest,
    ):
        return True

    previous = _fold_sales_text(_last_outbound_text(context))
    purchase_question = any(
        cue in previous
        for cue in (
            "quer contratar",
            "contratar esse plano",
            "quer seguir com o plano",
            "quer aproveitar e contratar",
            "contratar agora",
            "contratar com essa condicao",
            "quer avancar",
            "podemos avancar",
            "vamos avancar",
            "quer fechar",
            "podemos fechar",
            "vamos fechar",
            "dar andamento",
            "seguir com a contratacao",
        )
    )
    # Botões de avanço criados pela própria IA ("Avançar assim", "Quero
    # contratar") também são aceite. Antes, só frases fixas contavam e o
    # cliente que clicava no botão não era transferido (simulação 03/11).
    if purchase_question and re.fullmatch(
        r"(?:sim\s*)?(?:(?:pode|quero|vamos|bora|podemos)\s+)?"
        r"(?:avancar|seguir|fechar|contratar|prosseguir|continuar|bora|topo|eu topo)"
        r"(?:\s+(?:assim|agora|sim|com essa condicao|com ela|com esse plano))?",
        latest,
    ):
        return True
    return purchase_question and latest in {
        "sim",
        "sim quero",
        "quero",
        "pode",
        "vamos",
        "confirmo",
    }


# Caminho rápido para quem já chega pedindo para contratar um plano específico
# (ex.: anúncio "Tenho interesse em assinar 650mb por R$99,90"). Antes, essa
# intenção passava pelo modelo, pelas regras fixas e pelo Guardião; quando um
# deles divergia, o cliente ouvia "vou encaminhar" e a transferência não
# acontecia (conversas 239, 237, 72 e 68).
_FAST_PURCHASE_CITY_MARK = "para confirmar a disponibilidade do plano de"


def _ftth_plan_by_speed(speed: int | None) -> dict | None:
    if not speed:
        return None
    for plan in FTTH_PLANS:
        if int(plan.get("download_mbps") or 0) == int(speed):
            return plan
    return None


def _service_city_in_text(value: str | None) -> str:
    folded = _fold_sales_text(value)
    if not folded:
        return ""
    for service_city in sorted(SERVICE_CITIES, key=len, reverse=True):
        city_key = _normalized_city(service_city)
        if folded == city_key or re.search(rf"\b{re.escape(city_key)}\b", folded):
            return service_city
    return ""


def _known_city_for_fast_path(context: dict) -> str:
    facts_city = str((context.get("confirmed_facts") or {}).get("city") or "").strip()
    if facts_city:
        return facts_city
    lead_city = str((context.get("lead") or {}).get("city") or "").strip()
    if lead_city:
        return lead_city
    return _service_city_in_text(str(context.get("latest_customer_message") or ""))


def _ad_style_plan_interest(value: str | None) -> bool:
    """Mensagem no formato do anúncio: interesse + plano + preço na mesma frase."""
    folded = _fold_sales_text(value)
    return bool(
        re.search(r"\binteresse\b", folded)
        and re.search(r"\br\s*\d{2,3}\s*\d{2}\b", folded)
    )


def _fast_purchase_request(context: dict) -> dict | None:
    """Intenção explícita de compra de um plano FTTH conhecido.

    Devolve {"plan": <plano>, "city": <cidade ou "">} ou None. Considera a
    mensagem atual e, quando a última pergunta foi a de cidade deste caminho,
    a intenção registrada nas mensagens anteriores do cliente.
    """
    latest = str(context.get("latest_customer_message") or "")
    plan = None
    if _explicit_purchase_commitment(context) or _ad_style_plan_interest(latest):
        plan = _ftth_plan_by_speed(_requested_plan_speed(latest))

    if not plan:
        last_outbound = _fold_sales_text(_last_outbound_text(context))
        if _FAST_PURCHASE_CITY_MARK not in last_outbound:
            return None
        for row in reversed(context.get("history") or []):
            if str(row.get("direction") or "").upper() != "INBOUND":
                continue
            body = str(row.get("body") or "")
            if not (
                _explicit_purchase_commitment({"latest_customer_message": body, "history": []})
                or _ad_style_plan_interest(body)
            ):
                continue
            plan = _ftth_plan_by_speed(_requested_plan_speed(body))
            if plan:
                break
    if not plan:
        return None
    return {"plan": plan, "city": _known_city_for_fast_path(context)}


def _fast_purchase_decision(plan: dict, city: str, *, handoff: bool) -> AIDecision:
    speed = int(plan["download_mbps"])
    price = float(plan["price_fidelity"])
    plan_label = f"Fibra {speed} Mega"
    if handoff:
        reply = f"Perfeito! Vamos seguir com a contratação do {plan_label} por R$ {_format_brl(price)}."
        summary = (
            f"Intenção explícita de contratar {plan_label} por R$ {_format_brl(price)}"
            + (f" em {city}." if city else ".")
        )
    else:
        reply = (
            f"Ótimo! Para confirmar a disponibilidade do plano de {speed} Mega por "
            f"R$ {_format_brl(price)}, em qual cidade será a instalação?"
        )
        summary = f"Intenção explícita de contratar {plan_label}; aguardando a cidade."
    return AIDecision(
        reply_text=reply,
        intent="HIGH_INTENT",
        qualification_score=95 if handoff else 85,
        temperature="QUENTE",
        recommended_stage="NEGOCIACAO" if handoff else "QUALIFICADO",
        handoff_recommended=handoff,
        handoff_reason="Intenção explícita de compra." if handoff else "",
        confidence=0.95,
        summary=summary,
        next_action=(
            "Vendedor IA conduz o fechamento." if handoff else "Confirmar a cidade da instalação."
        ),
        qualification=QualificationData(
            desired_product=plan_label,
            city=city or None,
        ),
        sales_state="PURCHASE_INTENT" if handoff else "QUALIFIED",
        offer_plan_code=plan_label,
        offer_price=price,
        customer_commitment=handoff,
        followup_needed=not handoff,
        journey_state="CLOSING" if handoff else "CONFIRM_CITY",
        journey_pending_action=(
            "" if handoff else "Confirmar a cidade para validar a disponibilidade do plano."
        ),
        handoff_target="CLOSER_AI" if handoff else "NONE",
    )


def _journey_product_kind(decision: AIDecision, context: dict) -> str:
    facts = context.get("confirmed_facts") or {}
    fact_kind = str(facts.get("product_kind") or "").upper()
    if fact_kind in {"INTERNET", "MOBILE"}:
        return fact_kind

    latest_kind = _product_kind_from_text(
        str(context.get("latest_customer_message") or ""),
        previous_outbound=_last_outbound_text(context),
    )
    if latest_kind:
        return latest_kind

    memory = _journey_memory_from_context(context)
    combined = _fold_sales_text(
        " ".join(
            [
                str(decision.qualification.desired_product or ""),
                str(decision.offer_plan_code or ""),
                str(memory.get("desired_product") or ""),
                str(memory.get("offer_plan_code") or ""),
            ]
        )
    )
    if any(token in combined for token in ("sulmovel", "telefonia movel", "mobile", "movel")):
        return "MOBILE"
    if any(token in combined for token in ("internet", "fibra", "ftth", "corp", "corporativo", "conexao")):
        return "INTERNET"
    return ""


def _derive_journey_state(decision: AIDecision, context: dict) -> str:
    if decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT":
        return "OPT_OUT"
    # Pedido de "falar com uma pessoa" não muda a etapa: não existe atendimento
    # humano, a IA responde e segue a jornada (ai_sdr._never_hand_to_human).
    if _explicit_purchase_commitment(context):
        return "CLOSING"

    existing_customer = _existing_customer_from_context(context)
    if existing_customer not in {"CLIENTE", "NAO_CLIENTE"}:
        return "IDENTIFY_CUSTOMER"

    product_kind = _journey_product_kind(decision, context)
    if not product_kind:
        return "CHOOSE_PRODUCT"

    if product_kind == "MOBILE":
        if decision.offer_plan_code:
            return "HANDLE_OBJECTION" if decision.objection != "NONE" else "CONFIRM_PURCHASE"
        return "SELECT_OFFER"

    customer_type = _customer_type_from_context(context)
    if customer_type not in {"EMPRESA", "RESIDENCIAL"}:
        return "CHOOSE_SEGMENT"

    city = _city_from_sales_context(decision, context)
    if customer_type == "RESIDENCIAL" and not city:
        return "CONFIRM_CITY"

    if not decision.offer_plan_code:
        return "SELECT_OFFER"
    if decision.objection != "NONE":
        return "HANDLE_OBJECTION"
    return "CONFIRM_PURCHASE"


def _sdr_exposes_internal_handoff(text: str | None) -> bool:
    folded = _fold_sales_text(text)
    if not folded:
        return False
    internal_cues = (
        "vendedor ia",
        "vendedor de fechamento",
        "ia de fechamento",
        "o vendedor vai continuar",
        "o vendedor dara continuidade",
        "o vendedor seguira",
        "encaminhar seu interesse",
    )
    return any(cue in folded for cue in internal_cues)


def _journey_integrity_issues(
    decision: AIDecision,
    context: dict,
) -> list[str]:
    issues: list[str] = []
    state = str(decision.journey_state or "").upper()
    existing_customer = _existing_customer_from_context(context)
    product_kind = _journey_product_kind(decision, context)
    customer_type = _customer_type_from_context(context)
    city = _city_from_sales_context(decision, context)

    if existing_customer in {"CLIENTE", "NAO_CLIENTE"}:
        if state == "IDENTIFY_CUSTOMER":
            issues.append("STATE_REGRESSION_EXISTING_CUSTOMER")
        if _asks_existing_customer(decision.reply_text):
            issues.append("IGNORED_CONFIRMED_EXISTING_CUSTOMER")

    if product_kind in {"INTERNET", "MOBILE"} and state == "CHOOSE_PRODUCT":
        issues.append("STATE_REGRESSION_PRODUCT")

    if (
        product_kind == "INTERNET"
        and customer_type in {"EMPRESA", "RESIDENCIAL"}
        and state == "CHOOSE_SEGMENT"
    ):
        issues.append("STATE_REGRESSION_CUSTOMER_TYPE")

    if (
        product_kind == "INTERNET"
        and customer_type == "RESIDENCIAL"
        and city
        and state == "CONFIRM_CITY"
    ):
        issues.append("STATE_REGRESSION_CITY")

    if _sdr_reply_is_stale_repetition(decision, context):
        issues.append("REPEATED_QUESTION")

    if (
        _sdr_exposes_internal_handoff(decision.reply_text)
        and not (
            decision.handoff_target == "CLOSER_AI"
            and decision.customer_commitment
            and decision.journey_state == "CLOSING"
        )
    ):
        issues.append("SDR_EXPOSED_INTERNAL_HANDOFF")

    return list(dict.fromkeys(issues))


def _apply_stagnation_strategy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    if black.is_offer_question(decision.reply_text) or black.is_options_question(decision.reply_text):
        return decision  # etapa da campanha Black: não troca pela pergunta padrão

    state = decision.journey_state
    existing = _existing_customer_from_context(context)
    customer_type = _customer_type_from_context(context)
    previous_outbound = _recent_outbound_messages_for_repetition(context)

    if state == "IDENTIFY_CUSTOMER":
        decision = _show_existing_customer_question(decision)
        if previous_outbound:
            decision.reply_text = "Só para eu direcionar certinho: você já possui algum serviço da Sulnet?"
        return decision

    if state == "CHOOSE_PRODUCT":
        if existing == "CLIENTE":
            decision = _show_existing_customer_product_question(decision)
            if previous_outbound:
                decision.reply_text = "Para sua próxima contratação, prefere Telefonia Móvel ou internet em outro endereço?"
            return decision
        if existing == "NAO_CLIENTE":
            decision = _show_new_customer_product_question(decision)
            if previous_outbound:
                decision.reply_text = "Qual caminho você quer seguir primeiro: Internet ou Telefonia Móvel?"
            return decision

    if state == "CHOOSE_SEGMENT":
        decision = _show_internet_segment_question(
            decision,
            existing_customer=existing == "CLIENTE",
        )
        if previous_outbound:
            decision.reply_text = (
                "E essa nova conexão será residencial ou empresarial?"
                if existing == "CLIENTE"
                else "Essa conexão será residencial ou empresarial?"
            )
        return decision

    if state == "CONFIRM_CITY":
        decision.reply_text = (
            "Em qual cidade será instalada a conexão?"
            if previous_outbound
            else "Para eu te mostrar somente os planos disponíveis, qual é a sua cidade?"
        )
        decision.quick_replies = []
        decision.intent = "QUALIFYING"
        decision.sales_state = "DISCOVERY"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision

    if state == "SELECT_OFFER":
        if customer_type == "EMPRESA":
            decision = _show_corporate_catalog(decision)
            if previous_outbound:
                decision.reply_text = "Qual das opções corporativas você prefere avaliar agora?"
            return decision
        if _journey_product_kind(decision, context) == "MOBILE":
            decision = _show_mobile_catalog_page(decision, page=1)
            if previous_outbound:
                decision.reply_text = "Qual plano de Telefonia Móvel você quer avaliar agora?"
            return decision
        city = _city_from_sales_context(decision, context)
        if city:
            decision = _show_fiber_catalog(decision, context)
            if previous_outbound:
                decision.reply_text = f"Entre as opções disponíveis em {city}, qual velocidade você quer avaliar?"
            return decision

    if state == "HANDLE_OBJECTION":
        if decision.objection == "PRICE":
            decision.reply_text = "Para eu comparar de forma justa, quanto você paga hoje pela sua conexão?"
            decision.quick_replies = []
        else:
            decision.reply_text = "Para eu te ajudar por outro ângulo, o que mais pesa na sua decisão hoje?"
            decision.quick_replies = [
                QuickReply(id="prioridade_preco", label="Preço"),
                QuickReply(id="prioridade_qualidade", label="Qualidade"),
                QuickReply(id="prioridade_momento", label="Momento"),
            ]
        decision.intent = "QUALIFYING"
        decision.sales_state = "NEGOTIATING"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision

    if state == "CONFIRM_PURCHASE":
        decision.reply_text = "Para seguirmos daqui, você prefere contratar agora ou ver outra opção?"
        decision.quick_replies = [
            QuickReply(id="contratar_agora", label="Contratar agora"),
            QuickReply(id="ver_outra_opcao", label="Ver outra opção"),
        ]
        decision.intent = "INTEREST"
        decision.sales_state = "OFFERING"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.next_action = _JOURNEY_PENDING_ACTIONS[state]
        return decision

    return decision


def _apply_journey_orchestration_policy(
    decision: AIDecision,
    context: dict,
) -> AIDecision:
    state = _derive_journey_state(decision, context)
    decision.journey_state = state
    decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS.get(
        state,
        _JOURNEY_PENDING_ACTIONS["DISCOVERY"],
    )

    if state == "OPT_OUT":
        decision.handoff_target = "NONE"
        decision.handoff_recommended = False
        decision.followup_needed = False
    elif state == "HUMAN_HANDOFF":
        decision.handoff_target = "HUMAN"
        decision.handoff_recommended = True
        decision.intent = "HUMAN_REQUEST"
        decision.sales_state = "READY_FOR_HUMAN_SALES"
        decision.handoff_reason = (
            decision.handoff_reason
            or "Cliente pediu explicitamente atendimento humano."
        )
        decision.followup_needed = False
    elif state == "CLOSING":
        decision.handoff_target = "CLOSER_AI"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        decision.customer_commitment = True
        decision.sales_state = "PURCHASE_INTENT"
        if decision.intent in {"HUMAN_REQUEST", "UNKNOWN"}:
            decision.intent = "HIGH_INTENT"
        decision.next_action = _JOURNEY_PENDING_ACTIONS["CLOSING"]
        decision.followup_needed = False
    else:
        decision.handoff_target = "NONE"
        decision.handoff_recommended = False
        decision.handoff_reason = ""
        if decision.intent == "HUMAN_REQUEST":
            decision.intent = (
                "QUALIFYING"
                if state in {"IDENTIFY_CUSTOMER", "CHOOSE_PRODUCT", "CHOOSE_SEGMENT", "CONFIRM_CITY"}
                else "INTEREST"
            )
        decision.customer_commitment = False
        if decision.sales_state in {"READY_FOR_HUMAN_SALES", "PURCHASE_INTENT"}:
            decision.sales_state = "NEGOTIATING" if decision.objection != "NONE" else "OFFERING"

    mandatory_gate_states = {
        "IDENTIFY_CUSTOMER",
        "CHOOSE_PRODUCT",
        "CHOOSE_SEGMENT",
        "CONFIRM_CITY",
    }
    if state in mandatory_gate_states:
        decision = _apply_stagnation_strategy(decision, context)
        decision.journey_state = state
        decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS[state]
        decision.handoff_target = "NONE"
        decision.handoff_recommended = False
        decision.handoff_reason = ""

    previous = _journey_memory_from_context(context)
    previous_state = str(previous.get("state") or "")
    previous_pending = str(previous.get("pending_action") or "")
    previous_count = int(previous.get("stagnation_count") or 0)
    same_position = (
        previous_state == state
        and previous_pending == decision.journey_pending_action
        and state not in {"CLOSING", "HUMAN_HANDOFF", "OPT_OUT"}
    )
    decision.stagnation_count = min(20, previous_count + 1) if same_position else 0

    repetitive = _sdr_reply_is_stale_repetition(decision, context)
    if repetitive or decision.stagnation_count >= 3:
        decision = _apply_stagnation_strategy(decision, context)
        decision.journey_state = state
        decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS.get(
            state,
            _JOURNEY_PENDING_ACTIONS["DISCOVERY"],
        )
        decision.stagnation_count = 0 if repetitive else decision.stagnation_count

    if not decision.next_action.strip():
        decision.next_action = decision.journey_pending_action

    experiment = context.get("learning_experiment") or {}
    instruction = " ".join(
        str(experiment.get(key) or "")
        for key in ("baseline_instruction", "experiment_instruction")
    ).casefold()
    if (
        decision.journey_state == "CHOOSE_SEGMENT"
        and "casa ou empresa" in instruction
    ):
        decision.reply_text = "A internet é para sua casa ou empresa?"
        decision.quick_replies = [
            QuickReply(id="internet_casa", label="Para casa"),
            QuickReply(id="internet_empresa", label="Para empresa"),
        ]

    return decision

def _is_closer_handoff(decision: AIDecision) -> bool:
    return bool(
        decision.handoff_target == "CLOSER_AI"
        and decision.journey_state == "CLOSING"
        and decision.customer_commitment
    )


_PURCHASE_HESITATION_CUES = (
    "nao", "caro", "pensar", "depois", "duvida", "mas ", "porem", "sera que", "talvez",
)


def _offer_in_history(context: dict) -> bool:
    for row in context.get("history") or []:
        if str(row.get("direction") or "").upper() != "OUTBOUND":
            continue
        body = _fold_sales_text(str(row.get("body") or ""))
        if re.search(r"\b\d{3}\s*mega\b", body) and re.search(r"\br\s*\d{2,3}", body):
            return True
    return False


def _promote_clear_purchase_to_closing(decision: AIDecision, context: dict) -> AIDecision:
    """O modelo marcou intenção alta de compra depois de uma oferta, mas não
    transferiu: leva ao fechamento. Antes, o Guardião bloqueava por "intenção
    sem handoff" e o cliente ia para um humano (simulação e conversa 239)."""
    if decision.journey_state in {"CLOSING", "HUMAN_HANDOFF", "OPT_OUT"}:
        return decision
    if decision.intent != "HIGH_INTENT" and decision.sales_state != "PURCHASE_INTENT":
        return decision
    if not _offer_in_history(context) or _customer_asked_direct_question(context):
        return decision
    latest = _fold_sales_text(str(context.get("latest_customer_message") or ""))
    if not latest or any(cue in f" {latest} " for cue in _PURCHASE_HESITATION_CUES):
        return decision
    decision.journey_state = "CLOSING"
    decision.journey_pending_action = _JOURNEY_PENDING_ACTIONS["CLOSING"]
    decision.handoff_target = "CLOSER_AI"
    decision.handoff_recommended = False
    decision.handoff_reason = ""
    decision.customer_commitment = True
    decision.sales_state = "PURCHASE_INTENT"
    decision.intent = "HIGH_INTENT"
    decision.followup_needed = False
    return decision
