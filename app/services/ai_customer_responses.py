"""Respostas prioritarias do SDR: resolver a necessidade antes do script de vendas.

Este modulo opera somente sobre a conversa recebida e o catalogo oficial.
Nunca acessa outros numeros, usuarios, instancias ou historicos.
"""
from __future__ import annotations

import re
import unicodedata

from app.services.ai_customer_questions import (
    _fold, context_denies_city, home_wifi_answer,
)
from app.services.ai_sdr_models import AIDecision, QualificationData
from app.services.ai_sdr_policies import (
    _city_from_sales_context, _eligible_plans_for_city, _format_brl,
    _last_outbound_text, _plan_from_sales_context,
)
from app.services.ai_sdr_policy import PLAN_CHANGE_SERVICE_PHONE


_BILLING = re.compile(
    r"\b(?:segunda via|2 via|2a via|boleto|"
    r"(?:quero|preciso|gostaria) (?:de )?(?:receber|pagar|baixar|imprimir|solicitar) (?:a |minha |o )?fatura|"
    r"(?:pagar|receber|enviar|mandar|emitir) (?:a |minha |o )?fatura|"
    r"fatura (?:para |pra )?(?:pagar|pagamento))\b"
)
_ROUTER_PRICE = re.compile(
    r"\b(?:quanto|preco|valor|custa|fica|sai)\b.{0,70}"
    r"\b(?:2|dois|duas|segundo|adicional|extra) (?:roteadores?|aparelhos?)\b"
    r"|\b(?:preco|valor|custo)\b.{0,40}\b(?:segundo|adicional) roteador\b"
)
_BUDGET = re.compile(
    r"\b(?:quero|preciso|posso|gostaria) (?:de )?(?:pagar|manter|ficar em|ficar nos?)"
    r" (?:so |apenas |ate |no maximo |r )?(\d{2,4}(?:[.,]\d{1,2})?)\b"
    r"|\b(?:no maximo|limite de|orcamento de|ate) (?:r )?(\d{2,4}(?:[.,]\d{1,2})?)\b"
)
_PRICE_OBJECTION = re.compile(r"\b(?:caro|cara|muito caro|acima do que pago|nao vale|fora do orcamento)\b")
_CITY_CORRECTION = re.compile(r"\b(?:nao sou de|nao moro em|nao e em|nao estou em)\b")
_PLAN_SPEED = re.compile(r"\b(250|500|650|800)\s*(?:mega|mb|mbps)\b")


def _money_text(value: str | None) -> str:
    # Mantem virgula e ponto decimal, que _fold remove por ser para busca textual.
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"r\s*\$\s*", "", text.casefold())


def _result(
    text: str, *, summary: str, action: str,
    city: str = "", price: float | None = None, plan: str | None = None,
    billing: bool = False, objection: str = "NONE",
) -> AIDecision:
    result = AIDecision(
        reply_text=text,
        qualification_score=45 if billing else 65,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO" if billing else "PROPOSTA",
        handoff_recommended=False,
        confidence=1.0,
        summary=summary,
        next_action=action,
        intent="INFORMATION" if billing or objection == "NONE" else "OBJECTION_PRICE",
        sales_state="WAITING_CUSTOMER" if billing else "NEGOTIATING" if objection != "NONE" else "DISCOVERY",
        qualification=QualificationData(city=city or None),
        offer_plan_code=plan,
        offer_price=price,
        followup_needed=not billing,
        objection=objection,
        journey_state="DISCOVERY" if billing else "HANDLE_OBJECTION" if objection != "NONE" else "DISCOVERY",
        journey_pending_action=action,
        service_redirect=billing,
        unanswered_question=None,
    )
    result._contextual_reply = True
    return result


def _home(context: dict) -> AIDecision | None:
    answer = home_wifi_answer(context)
    if not answer:
        return None
    return _result(
        answer, summary="Duvida sobre alcance do Wi-Fi dentro de casa respondida sem garantia tecnica.",
        action="Aguardar quantidade de pisos ou metragem para avaliar a distribuicao do Wi-Fi.",
    )


def _invoice(context: dict) -> AIDecision | None:
    latest = _fold(context.get("latest_customer_message"))
    if not _BILLING.search(latest) or "primeira fatura" in latest or "1a fatura" in latest:
        return None
    return _result(
        "Entendi, você precisa receber uma fatura para pagamento. "
        f"Se for da Sulnet, solicite a segunda via com segurança pelo WhatsApp da Central: {PLAN_CHANGE_SERVICE_PHONE}. "
        "Toque em Falar com a central para continuar, sem precisar enviar CPF ou documentos neste chat.",
        summary="Cliente solicitou boleto/fatura: direcionado ao canal financeiro oficial, sem prospeccao.",
        action="Aguardar atendimento financeiro no WhatsApp oficial da Central.",
        billing=True,
    )


def _city(context: dict) -> str:
    # Reusa a politica unica de validacao, inclusive negativas e cidades ja corrigidas.
    blank = _result("Consulta", summary="", action="")
    return _city_from_sales_context(blank, context)


def _router_price(context: dict) -> AIDecision | None:
    latest = _fold(context.get("latest_customer_message"))
    if not _ROUTER_PRICE.search(latest):
        return None
    city = _city(context)
    stub = _result("Consulta", summary="", action="", city=city)
    plan = _plan_from_sales_context(stub, context)
    speed = int(plan["download_mbps"]) if plan else 0
    base_price = float(plan["price_fidelity"]) if plan and speed in {650, 800} else None
    extra = float(plan.get("additional_router_monthly_price") or 29.90) if plan else 29.90

    if base_price is None:
        text = (
            f"Um roteador está incluso nos planos residenciais elegíveis; o segundo custa R$ {_format_brl(extra)} por mês. "
            "Qual velocidade você está avaliando, 650 ou 800 Mega?"
        )
    elif not city or not any(int(p["download_mbps"]) == speed for p in _eligible_plans_for_city(city)):
        text = (
            f"No plano de {speed} Mega, um roteador é incluso e o segundo custa R$ {_format_brl(extra)} por mês. "
            "Para confirmar o valor total e a disponibilidade, em qual cidade será a instalação?"
        )
    else:
        text = (
            f"No plano de {speed} Mega, um roteador já está incluso. "
            f"Com o segundo, o valor fica em R$ {_format_brl(base_price + extra)} por mês "
            f"(R$ {_format_brl(base_price)} do plano + R$ {_format_brl(extra)} do roteador adicional). "
            "Sua casa tem mais de um andar?"
        )
    return _result(
        text, summary="Cliente recebeu valor transparente de um segundo roteador.",
        action="Avaliar necessidade real do roteador adicional antes de fechar.",
        city=city, price=base_price, plan=plan.get("code") if plan else None,
    )


def _current_plan_from_history(context: dict) -> tuple[int | None, float | None, bool]:
    """Somente dados INBOUND, associados a perguntas reais do atendente."""
    speed: int | None = None
    price: float | None = None
    good_wifi = False
    last_out = ""
    for row in (context.get("history") or [])[-80:]:
        body = _fold(row.get("body"))
        money_body = _money_text(str(row.get("body") or ""))
        if str(row.get("direction") or "").upper() == "OUTBOUND":
            last_out = body
            continue
        if str(row.get("direction") or "").upper() != "INBOUND":
            continue
        if ("velocidade" in last_out and ("paga hoje" in last_out or "internet atual" in last_out)):
            match = _PLAN_SPEED.search(body)
            if match:
                speed = int(match.group(1))
        if ("paga" in last_out or "quanto voce paga" in last_out) and "hoje" in last_out:
            match = re.fullmatch(r"\s*(?:r\s*)?(\d{2,4}(?:[.,]\d{1,2})?)\s*", money_body)
            if match:
                price = float(match.group(1).replace(",", "."))
        match = re.search(r"\b(?:eu pago|pago hoje|pago atualmente)\s*(?:r )?(\d{2,4}(?:[.,]\d{1,2})?)\b", money_body)
        if match:
            price = float(match.group(1).replace(",", "."))
        match = re.search(r"\b(?:tenho|uso|minha internet e de)\s*(\d{2,4})\s*(?:mega|mb|mbps)\b", body)
        if match:
            speed = int(match.group(1))
        if body in {"sim", "sim pega", "pega"} and ("wifi" in last_out or "wi fi" in last_out) and "todos os comodos" in last_out:
            good_wifi = True
    return speed, price, good_wifi


def _price_target(context: dict) -> AIDecision | None:
    latest = _fold(context.get("latest_customer_message"))
    match = _BUDGET.search(_money_text(str(context.get("latest_customer_message") or "")))
    if not match:
        return None
    target = float(next(p for p in match.groups() if p).replace(",", "."))
    if target <= 0 or target >= 10000:
        return None
    city = _city(context)
    if not city:
        return _result(
            f"Entendi que você quer pagar no máximo R$ {_format_brl(target)}. "
            "Qual é a cidade da instalação para comparar somente as condições disponíveis?",
            summary="Cliente informou orcamento maximo; cidade nao confirmada.",
            action="Confirmar cidade antes de prometer preco.",
            objection="PRICE",
        )
    plans = _eligible_plans_for_city(city)
    available = sorted(
        (p for p in plans if float(p["price_fidelity"]) <= target + 0.001),
        key=lambda p: int(p["download_mbps"]), reverse=True,
    )
    current_speed, current_price, good_wifi = _current_plan_from_history(context)
    if not available:
        return _result(
            f"Entendi seu limite de R$ {_format_brl(target)}. "
            f"No momento, não tenho uma opção de fibra em {city} confirmada dentro desse valor. "
            "Você prefere avaliar outro orçamento ou manter o plano atual?",
            summary="Limite informado abaixo das ofertas oficiais da cidade.",
            action="Cliente decidir sem promessa de desconto nao autorizado.",
            city=city, objection="PRICE",
        )
    plan = available[0]
    speed = int(plan["download_mbps"])
    price = float(plan["price_fidelity"])
    if current_speed and current_speed > speed:
        qualifier = (
            f"Como você já tem {current_speed} Mega"
            + (f" por R$ {_format_brl(current_price)}" if current_price else "")
            + ", essa opção tem velocidade menor que a sua atual. "
        )
    else:
        qualifier = ""
    intro = "Entendi seu limite de gasto. "
    if good_wifi:
        intro += "Como o Wi-Fi atual já alcança a casa, não vou insistir em um segundo roteador. "
    text = (
        intro + f"Em {city}, há {speed} Mega por R$ {_format_brl(price)}/mês, com um roteador incluso. "
        + qualifier
        + "Você prefere avaliar essa diferença ou manter a conexão atual?"
    )
    return _result(
        text, summary="Objeção tratada com valor teto, comparacao real e sem desconto inventado.",
        action="Aguardar escolha do cliente; nao reiniciar qualificacao.",
        city=city, price=price, plan=plan["code"], objection="PRICE",
    )


def _city_correction(context: dict) -> AIDecision | None:
    latest = _fold(context.get("latest_customer_message"))
    if not _CITY_CORRECTION.search(latest):
        return None
    candidate = str((context.get("confirmed_facts") or {}).get("city")
        or (context.get("lead") or {}).get("city") or "").strip()
    if not candidate or not context_denies_city(context, candidate):
        return None
    return _result(
        f"Desculpe por usar {candidate} sem confirmar. Entendi que não é nessa cidade. "
        "Em qual cidade será a instalação?",
        summary="Cliente corrigiu localidade anteriormente presumida.",
        action="Confirmar a cidade informada pelo cliente.",
    )


def priority_reply(context: dict) -> AIDecision | None:
    """Prioridades fixas e restritas: suporte, pergunta objetiva, custo e correcao."""
    result = (
        _invoice(context)
        or _home(context)
        or _router_price(context)
        or _price_target(context)
        or _city_correction(context)
    )
    if result is None or result.service_redirect:
        return result
    # Nao apaga o produto/plano da jornada quando a resposta e apenas tecnica.
    if not result.offer_plan_code:
        plan = _plan_from_sales_context(result, context)
        if plan:
            result.offer_plan_code = str(plan["code"])
            result.offer_price = float(plan["price_fidelity"])
    facts = context.get("confirmed_facts") or {}
    journey = (context.get("lead") or {}).get("journey") or {}
    result.qualification.desired_product = (
        str(facts.get("desired_product") or journey.get("desired_product") or "") or None
    )
    return result
