"""Respostas do SDR para consultas diretas ao catálogo comercial."""

from __future__ import annotations

import re

from app.services import ai_campaign_black as black
from app.services.ai_customer_questions import context_denies_city
from app.services.ai_quick_replies import QuickReply
from app.services.ai_sdr_models import AIDecision, QualificationData
from app.services.ai_sdr_policies import (
    _EXPLICIT_CONTACT_STOP_CUES,
    _confirmed_city_from_text,
    _customer_type_from_context,
    _explicit_human_request,
    _explicit_purchase_commitment,
    _fold_sales_text,
    _format_brl,
    _last_outbound_text,
    _product_already_in_talk,
    _product_kind_from_text,
)
from app.services.ai_sdr_policy import FTTH_PLANS, MOBILE_PLANS, catalog_for

_PRICE_OR_DUE = re.compile(
    r"\b(?:preco|valor(?:es)?|quanto (?:sai|fica|custa|pago|da)|custa quanto|"
    r"mensalidade|vencimento|primeira (?:fatura|cobranca|mensalidade)|1a (?:fatura|cobranca|mensalidade)|"
    r"quando (?:comeco a pagar|pago|vence)|mais barat[oa]|mais em conta|economico|orcamento|cotacao)\b",
)
_INTEREST = re.compile(r"\b(?:quero|queria|gostaria|preciso|interesse|quais planos|mostrar (?:as )?opcoes)\b")
_FIBER_ONLY = re.compile(r"\b(?:sem (?:chip|movel)|(?:so|apenas|somente) (?:a )?(?:fibra|internet)|fibra avulsa)\b")
_CONTRACT_QUESTION = re.compile(r"\b(?:multa|proporcional|rescisao|atrasada|em atraso|vencida|sem fidelidade)\b")
_COMPARE = re.compile(
    r"\b(?:avulsa|so fibra|sem (?:chip|movel))\b.{0,60}\b(?:combo|com (?:chip|movel))\b"
    r"|\b(?:combo|com (?:chip|movel))\b.{0,60}\b(?:avulsa|so fibra|sem (?:chip|movel))\b",
)
_FIBER_NUMBER = r"\b(\d{2,4})\s*(?:megas?|mbps|mb)\b"
_MOBILE_NUMBER = r"\b(\d{1,3})\s*(?:gb|gigas?)\b"
_CHEAPER = re.compile(r"\b(?:mais barat[oa]|mais em conta|economico)\b")
_REGULAR_DUE = black.ORDINARY_FIRST_DUE


def _plan_number(pattern: str, folded: str) -> int | None:
    values = set(re.findall(pattern, folded))
    return int(next(iter(values))) if len(values) == 1 else None


def _remembered_plan_number(context: dict, pattern: str, folded: str) -> int | None:
    if re.search(pattern, folded):
        return _plan_number(pattern, folded)
    if _CHEAPER.search(folded):
        return None
    for row in reversed((context.get("history") or [])[-25:]):
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        earlier = _fold_sales_text(row.get("body"))
        if re.search(r"\b(?:meu plano atual|minha internet atual|eu tenho|tenho hoje)\b", earlier):
            continue
        if re.search(pattern, earlier) and (
            _INTEREST.search(earlier) or _PRICE_OR_DUE.search(earlier) or re.fullmatch(pattern, earlier)
        ):
            return _plan_number(pattern, earlier)
    return _plan_number(pattern, _fold_sales_text(_last_outbound_text(context)))


def _context_city(context: dict, latest: str, previous: str) -> str:
    facts = context.get("confirmed_facts") or {}
    candidate = (
        _confirmed_city_from_text(latest, previous)
        or str(facts.get("city") or "").strip()
    )
    return candidate if candidate and not context_denies_city(context, candidate) else ""


def _quote_decision(
    reply: str, *, city: str, segment: str, product: str, plan: dict | None = None,
    buttons: list[QuickReply] | None = None,
) -> AIDecision:
    decision = AIDecision(
        reply_text=reply, quick_replies=buttons or [],
        intent="INFORMATION", qualification_score=56, temperature="MORNO",
        recommended_stage="PROPOSTA", sales_state="OFFERING",
        handoff_recommended=False, confidence=1.0, summary="Consulta respondida com as condições do catálogo vigente.",
        next_action="Aguardar a escolha do cliente para avançar com a oferta.",
        journey_state="CONFIRM_PURCHASE" if plan else "SELECT_OFFER",
        journey_pending_action="Confirmar se o cliente quer seguir com a opção apresentada.",
        offer_plan_code=plan["code"] if plan else None,
        offer_price=(plan.get("price_with_fidelity") or plan.get("price")) if plan else None,
        qualification=QualificationData(
            city=city or None, customer_type=segment or "NAO_INFORMADO", desired_product=product,
        ),
        proposal_summary=reply[:800],
    )
    decision._contextual_reply = True
    return decision


def _check_out_buttons(product: str) -> list[QuickReply]:
    return [
        QuickReply(id="quero_contratar", label="Quero contratar"),
        QuickReply(id=f"ver_outros_{product}", label="Ver outros planos"),
    ]


def _price_text(plan: dict) -> str:
    return f"R$ {_format_brl(plan.get('price_with_fidelity') or plan['price'])}"


def _mobile_pricing(folded: str, gb: int | None, *, active_black: bool) -> AIDecision:
    plans = catalog_for("", "MOVEL")["plans"]
    plan = next((row for row in plans if row["gb"] == gb), None) if gb else min(plans, key=lambda row: row["price"])
    preface = ""
    if plan is None:
        plan = min(plans, key=lambda row: abs(row["gb"] - gb))
        preface = f"O plano de {gb}GB não consta no catálogo atual. "
    requested_app = "Waze/Maps" if ("maps" in folded or "waze" in folded) else "WhatsApp" if "whatsapp" in folded else ""
    if requested_app and not gb:
        eligible = [row for row in plans if requested_app in row["unlimited_apps"]]
        if eligible:
            plan = min(eligible, key=lambda row: row["price"])
    # A seleção leva à contratação. A data da Black vem da mesma fonte
    # usada no registro das duas contratações no CRM.
    text = preface + f"O {plan['name']} custa {_price_text(plan)}/mês."
    apps = plan["unlimited_apps"]
    if apps:
        text += f" Inclui {' e '.join(apps)} sem consumir a franquia."
    if active_black:
        text += (
            f" Na Telefonia Móvel da Black Sulnet, a 1ª mensalidade vence em {black.FIRST_DUE_DATE} "
            f"para contratações até {black.VALID_UNTIL_LABEL}."
        )
    else:
        text += f" {_REGULAR_DUE}"
    return _quote_decision(
        f"{text} Quer contratar esse plano?", city="", segment="", product=plan["name"], plan=plan,
        buttons=_check_out_buttons("movel"),
    )


def _internet_pricing(city: str, segment: str, plans: list[dict], folded: str, speed: int | None) -> AIDecision:
    if speed is None and _CHEAPER.search(folded):
        speed = min(plans, key=lambda row: row.get("price_with_fidelity") or row["price"])["speed"]
    matching = [row for row in plans if row["speed"] == speed] if speed else []
    visible = matching or plans
    label = "fibra residencial avulsa" if segment == "RESIDENCIAL" else "fibra corporativa"
    terms = " com fidelidade de 12 meses" if segment == "RESIDENCIAL" else ""
    preface = f"O plano de {speed} Mega não está disponível em {city}. " if speed and not matching else ""
    if len(visible) == 1:
        plan = visible[0]
        text = f"Em {city}, a {label} de {plan['speed']} Mega fica {_price_text(plan)}/mês{terms}."
        return _quote_decision(
            f"{preface}{text} {_REGULAR_DUE} Quer contratar esse plano?",
            city=city, segment=segment, product=label, plan=plan, buttons=_check_out_buttons("fibra"),
        )
    lines = [f"• {plan['speed']} Mega por {_price_text(plan)}/mês{terms}" for plan in visible]
    text = "\n".join([
        f"{preface}Em {city}, estas são as opções de {label}.",
        *lines, _REGULAR_DUE, "Qual plano você prefere?",
    ])
    return _quote_decision(
        text, city=city, segment=segment, product=label,
        buttons=[QuickReply(id=f"plano_{plan['speed']}", label=f"{plan['speed']} Mega") for plan in visible[:3]],
    )


def _combo_pricing(
    city: str, segment: str, plans: list[dict], speed: int | None, gb: int | None, *,
    compare: bool, active_black: bool,
) -> AIDecision:
    fiber = next((plan for plan in plans if plan["speed"] == speed), None) if speed else plans[0]
    mobile_plans = catalog_for("", "MOVEL")["plans"]
    mobile = next((plan for plan in mobile_plans if plan["gb"] == gb), None) if gb else mobile_plans[0]
    if fiber is None or mobile is None:
        if fiber is None:
            text = f"O plano de {speed} Mega não está disponível em {city}. Qual destas opções de fibra você prefere?"
            buttons = [QuickReply(id=f"plano_{row['speed']}", label=f"{row['speed']} Mega") for row in plans[:3]]
        else:
            text = f"O plano móvel de {gb}GB não consta no catálogo atual. Qual franquia você quer no combo?"
            buttons = [QuickReply(id=f"movel_{row['gb']}", label=f"{row['gb']}GB") for row in mobile_plans[:3]]
        return _quote_decision(text, city=city, segment=segment, product="Fibra + Telefonia Móvel", buttons=buttons)
    total = round((fiber.get("price_with_fidelity") or fiber["price"]) + mobile["price"], 2)
    label = "Fibra" if segment == "RESIDENCIAL" else "Fibra corporativa"
    terms = " com fidelidade de 12 meses" if segment == "RESIDENCIAL" else ""
    lines = [
        f"{'Combo Black da Sulnet' if active_black else 'Fibra e Sulmóvel'} em {city}",
        f"• {label} {fiber['speed']} Mega por {_price_text(fiber)}/mês{terms}",
        f"• {mobile['name']} por {_price_text(mobile)}/mês",
        f"• Total de R$ {_format_brl(total)}/mês",
    ]
    if active_black:
        lines.append(
            f"A 1ª mensalidade dos dois vence em {black.FIRST_DUE_DATE} ao contratar os dois serviços "
            f"até {black.VALID_UNTIL_LABEL}."
        )
    else:
        lines.append(_REGULAR_DUE)
    if compare:
        lines.append(f"Só a fibra fica {_price_text(fiber)}/mês. {_REGULAR_DUE}")
    if not active_black:
        lines.append("Quer começar pela fibra avulsa?")
        buttons = [QuickReply(id="fibra_avulsa", label="Só fibra"), QuickReply(id="cliente_movel", label="Só móvel")]
    elif compare:
        lines.append("Qual das opções você prefere?")
        buttons = [QuickReply(id="black_combo", label="Quero o combo"), QuickReply(id="fibra_avulsa", label="Só fibra")]
    else:
        lines.append("Quer contratar esse combo?")
        buttons = [QuickReply(id="black_combo", label="Quero o combo"), QuickReply(id="fibra_avulsa", label="Só fibra")]
    return _quote_decision(
        "\n".join(lines), city=city, segment=segment, product="Fibra + Telefonia Móvel", plan=fiber, buttons=buttons,
    )


def expired_black_acceptance(context: dict) -> bool:
    previous = _fold_sales_text(_last_outbound_text(context))
    latest = _fold_sales_text(context.get("latest_customer_message"))
    return (
        (black.QUOTE_COMBO_MARK in previous or black.QUOTE_MOBILE_MARK in previous)
        and not black.black_active()
        and (_explicit_purchase_commitment(context) or latest in black.COMBO_ACCEPTANCES)
    )


def _quote_preference(context: dict) -> tuple[str, str, str, bool, bool] | None:
    latest = str(context.get("latest_customer_message") or "")
    folded = _fold_sales_text(latest)
    if not folded or _CONTRACT_QUESTION.search(folded) or _explicit_human_request(context):
        return None
    # 'Quanto fica com dois roteadores?' nao e pedido de tabela de planos.
    if "roteador" in folded and re.search(
        r"\b(?:quanto|preco|valor|fica|custa|sai)\b", folded
    ):
        return None
    if any(cue in folded for cue in _EXPLICIT_CONTACT_STOP_CUES) or re.search(
        r"\b(?:nao (?:quero|tenho) (?:mais|interesse)|desisti|dispenso)\b", folded,
    ):
        return None
    previous = _last_outbound_text(context)
    previous_folded = _fold_sales_text(previous)
    active_black = black.black_active()
    expired = expired_black_acceptance(context)
    city = _context_city(context, latest, previous)
    segment = _customer_type_from_context(context)
    facts = context.get("confirmed_facts") or {}
    compare = bool(_COMPARE.search(folded)) or (
        "qual das opcoes voce prefere" in previous_folded
        and black.QUOTE_COMBO_MARK in previous_folded
        and _explicit_purchase_commitment(context)
        and folded not in black.COMBO_ACCEPTANCES
        and not _FIBER_ONLY.search(folded)
    )
    kind = (
        "COMBO" if compare else _product_kind_from_text(latest, previous_outbound=previous)
        or facts.get("product_kind") or _product_already_in_talk(context)
    )
    if expired:
        kind = "COMBO" if black.QUOTE_COMBO_MARK in previous_folded else "MOBILE"
    choosing_fiber = bool(re.fullmatch(_FIBER_NUMBER, folded) and "fibra" in previous_folded)
    choosing_mobile = bool(re.fullmatch(_MOBILE_NUMBER, folded) and "sulmovel" in previous_folded)
    if not kind and (choosing_fiber or choosing_mobile):
        kind = "INTERNET" if choosing_fiber else "MOBILE"
    if not kind and _PRICE_OR_DUE.search(folded):
        if _plan_number(_MOBILE_NUMBER, folded):
            kind = "MOBILE"
        elif _plan_number(_FIBER_NUMBER, folded):
            kind = "INTERNET"
    answered_city = bool(
        city and _confirmed_city_from_text(latest, previous) == city
        and "qual" in previous_folded and "cidade" in previous_folded
    )
    answered_segment = folded in {"casa", "para casa", "empresa", "para empresa", "residencial", "empresarial"}
    requests_quote = (
        expired or compare or _FIBER_ONLY.search(folded) or _PRICE_OR_DUE.search(folded)
        or (_INTEREST.search(folded) and _product_kind_from_text(latest))
        or answered_city or answered_segment or choosing_fiber or choosing_mobile
    )
    if not requests_quote:
        return None
    return kind, city, segment, compare, active_black


def contextual_catalog_reply(context: dict) -> AIDecision | None:
    requested = _quote_preference(context)
    if requested is None:
        return None
    kind, city, segment, compare, active_black = requested
    folded = _fold_sales_text(context.get("latest_customer_message"))
    if kind == "MOBILE":
        return _mobile_pricing(folded, _remembered_plan_number(context, _MOBILE_NUMBER, folded), active_black=active_black)
    if kind not in {"INTERNET", "COMBO"}:
        return None
    if not (city and segment):
        product = "Fibra + Telefonia Móvel" if kind == "COMBO" else "Fibra"
        question = "Para consultar os valores certos, a fibra é para sua casa ou para uma empresa?"
        buttons = [QuickReply(id="internet_casa", label="Para casa"), QuickReply(id="internet_empresa", label="Para empresa")]
        if segment:
            question = "Em qual cidade seria a instalação? Assim consulto os planos disponíveis."
            buttons = []
        decision = _quote_decision(question, city=city, segment=segment, product=product, buttons=buttons)
        decision.intent = "QUALIFYING"
        decision.recommended_stage = "CONTATO_REALIZADO"
        decision.sales_state = "DISCOVERY"
        decision.summary = "Cliente pediu uma oferta do catálogo."
        decision.journey_state = "CONFIRM_CITY" if segment else "CHOOSE_SEGMENT"
        decision.next_action = "Confirmar a cidade da instalação." if segment else "Confirmar se é casa ou empresa."
        decision.journey_pending_action = decision.next_action
        decision.proposal_summary = ""
        return decision
    catalog = catalog_for(city, segment)
    if not catalog["plans"]:
        return _quote_decision(
            f"{city} está fora da área de atuação da fibra Sulnet. A instalação seria em outra cidade?",
            city=city, segment=segment, product="Fibra",
        )
    speed = _remembered_plan_number(context, _FIBER_NUMBER, folded)
    if kind == "COMBO":
        return _combo_pricing(
            city, segment, catalog["plans"], speed, _remembered_plan_number(context, _MOBILE_NUMBER, folded),
            compare=compare, active_black=active_black and segment == "RESIDENCIAL",
        )
    return _internet_pricing(city, segment, catalog["plans"], folded, speed)


def accepted_black_quote(context: dict) -> dict | None:
    """Recupera os dois produtos de um orçamento que chegou ao histórico de envio."""
    if not black.black_active():
        return None
    previous = _last_outbound_text(context)
    folded = _fold_sales_text(previous)
    latest_message = str(context.get("latest_customer_message") or "")
    latest = _fold_sales_text(latest_message)
    if "?" in latest_message or _explicit_human_request(context) or any(cue in latest for cue in _EXPLICIT_CONTACT_STOP_CUES):
        return None
    if not (_explicit_purchase_commitment(context) or latest in black.COMBO_ACCEPTANCES):
        return None
    if re.search(r"\b(?:mas|porem|se|talvez|depois|so fibra|sem chip|sem movel)\b", latest):
        return None
    gb = _plan_number(_MOBILE_NUMBER, folded)
    mobile = next((plan for plan in MOBILE_PLANS if plan["data_gb"] == gb), None)
    if not mobile or _price_text(mobile) not in previous:
        return None
    requested_gb = _plan_number(_MOBILE_NUMBER, latest)
    if requested_gb and requested_gb != gb:
        return None
    if black.QUOTE_MOBILE_MARK in folded:
        return {"plan": mobile, "city": "", "mobile_gb": None}
    if black.QUOTE_COMBO_MARK not in folded or (
        "qual das opcoes" in folded and latest not in black.COMBO_ACCEPTANCES
    ):
        return None
    city = _context_city(context, latest, previous)
    speed = _plan_number(_FIBER_NUMBER, folded)
    requested_speed = _plan_number(_FIBER_NUMBER, latest)
    if requested_speed and requested_speed != speed:
        return None
    available = catalog_for(city, "RESIDENCIAL") if city else {}
    if not any(plan["speed"] == speed for plan in available.get("plans", [])):
        return None
    fiber = next((plan for plan in FTTH_PLANS if plan["download_mbps"] == speed), None)
    if not fiber or f"R$ {_format_brl(fiber['price_fidelity'])}" not in previous:
        return None
    return {"plan": fiber, "city": city, "mobile_gb": gb}
