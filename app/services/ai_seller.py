from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Activity,
    AIAgentProfile,
    AIClosingState,
    AIConversationState,
    AIFollowupJob,
    AIJob,
    AIKnowledgeItem,
    AIKnowledgeMetadata,
    AIRun,
    Conversation,
    ConversationAssignment,
    Lead,
    Message,
)
from app.services.ai_guardian import GuardianBlockedError, guard_outbound_message
from app.services.ai_sdr_policy import FTTH_PLANS, PLAN_CHANGE_SERVICE_PHONE
from app.services.audit import write_audit
from app.services import ai_campaign_black as black_campaign
from app.services.crm_sulnet import (
    CRMIntegrationError,
    CRMPlaybookMissingItem,
    CRMPlaybookResult,
    SulnetCRMClient,
)
from app.services.queue_routing import assign_queue_user, queue_for_conversation
from app.services.zapi import ZApiError
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_seller")


class SellerInterpretation(BaseModel):
    confirmation: Literal["SIM", "NAO", "NAO_INFORMADO"] = "NAO_INFORMADO"
    requested_plan: str | None = Field(default=None, max_length=180)
    payment_method: str | None = Field(default=None, max_length=100)
    due_day: int | None = Field(default=None, ge=1, le=31)
    invoice_delivery: str | None = Field(default=None, max_length=100)
    human_requested: bool = False
    wants_contest: bool = False


def seller_operational() -> bool:
    return bool(
        settings.ai_seller_enabled
        and settings.ai_autonomous_enabled
        and settings.openai_api_key
        and settings.sulnet_crm_base_url
        and settings.sulnet_crm_bearer_token
    )


def active_closer(db: Session) -> AIAgentProfile | None:
    if not seller_operational():
        return None
    return db.scalar(
        select(AIAgentProfile).where(
            AIAgentProfile.role_type == "CLOSER",
            AIAgentProfile.active.is_(True),
            AIAgentProfile.execution_mode == "AUTO",
        ).order_by(AIAgentProfile.id.asc())
    )


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFD", (value or "").strip().lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money_for_api(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _redact(value: str) -> str:
    text = value or ""
    text = re.sub(r"(?<!\d)\d{15,22}(?!\d)", "[DADO_MOVEL]", text)
    text = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "[DOCUMENTO]", text)
    text = re.sub(r"(?<!\d)\d{5}-?\d{3}(?!\d)", "[CEP]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    return text[:1200]


def _load_collected(state: AIClosingState) -> dict[str, Any]:
    try:
        parsed = json.loads(state.collected_json or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_collected(state: AIClosingState, collected: dict[str, Any]) -> None:
    state.collected_json = json.dumps(collected, ensure_ascii=False, default=str)


def _looks_like_full_name(value: str) -> bool:
    clean = re.sub(r"\s+", " ", (value or "").strip())
    if len(clean) < 5 or len(clean) > 120:
        return False
    parts = [p for p in clean.split(" ") if p]
    return len(parts) >= 2 and all(any(ch.isalpha() for ch in p) for p in parts)


def _extract_name(text: str) -> str | None:
    clean = re.sub(r"^(meu nome (e|é)|sou|aqui e|aqui é)\s+", "", (text or "").strip(), flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" .,!;:")
    return clean if _looks_like_full_name(clean) else None


def _extract_cpf(text: str) -> str | None:
    digits = _digits(text)
    if len(digits) in {11, 14}:
        return digits
    candidates = re.findall(r"(?:\d[.\-\s]?){11,14}", text or "")
    for item in candidates:
        d = _digits(item)
        if len(d) in {11, 14}:
            return d
    return None


def _seller_ftth_plan(state: AIClosingState, collected: dict[str, Any]) -> dict[str, Any] | None:
    if _is_mobile_plan(state, collected):
        return None
    haystack = " ".join(
        str(value or "")
        for value in (
            state.plan_label,
            collected.get("planLabel"),
            collected.get("catalogPlanName"),
            collected.get("productName"),
            collected.get("offerPlanCode"),
        )
    )
    numbers = {int(value) for value in re.findall(r"(?<!\d)(250|500|650|800)(?!\d)", haystack)}
    for plan in FTTH_PLANS:
        if int(plan.get("download_mbps") or 0) in numbers:
            return plan
    return None


def _seller_brl(value: float | int | Decimal) -> str:
    return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_SELLER_ORDINAL_MONTHS = {
    "primeiro": 1,
    "segundo": 2,
    "terceiro": 3,
    "quarto": 4,
    "quinto": 5,
    "sexto": 6,
    "setimo": 7,
    "oitavo": 8,
    "nono": 9,
    "decimo": 10,
    "decimo primeiro": 11,
    "decimo segundo": 12,
}


def _seller_contract_months_used(text: str) -> int | None:
    key = _norm(text)
    matches = re.findall(r"(?<!\d)(\d{1,2})\s*[ºªo]?\s*mes(?:es)?\b", key)
    for raw in matches:
        months = int(raw)
        if 0 <= months <= 12:
            return months
    for word, months in sorted(_SELLER_ORDINAL_MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{word}\s+mes\b", key):
            return months
    return None


def _seller_bare_months_answer(text: str) -> int | None:
    """Resposta curta ao "com quantos meses?", como "7" ou "7 meses"."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*(?:[ºªo]\s*)?(?:mes(?:es)?)?\s*[.!]?\s*", _norm(text))
    if not match:
        return None
    months = int(match.group(1))
    return months if 0 <= months <= 12 else None


def _seller_free_months(state: AIClosingState, collected: dict[str, Any]) -> int:
    for key in ("freeMonths", "free_months", "mesesGratis", "meses_gratis"):
        if key in collected:
            try:
                return max(0, min(3, int(collected.get(key) or 0)))
            except (TypeError, ValueError):
                pass

    summary = _norm(str(getattr(state, "handoff_summary", "") or ""))
    match = re.search(r"meses? gratis\s*[=:]?\s*([0-3])", summary)
    if match:
        return int(match.group(1))
    return 0


def _seller_contract_penalty_value(
    state: AIClosingState,
    collected: dict[str, Any],
    plan: dict[str, Any],
    *,
    months_used: int,
) -> tuple[Decimal, Decimal, int, int]:
    remaining = max(0, 12 - int(months_used))
    monthly = Decimal(str(
        getattr(state, "offer_price", None)
        or plan.get("price_fidelity")
        or 0
    ))
    free_months = _seller_free_months(state, collected)
    total_benefit = Decimal("860.00") + (monthly * Decimal(free_months))
    penalty = (
        (total_benefit / Decimal("12")) * Decimal(remaining)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return penalty, total_benefit, remaining, free_months


def _seller_contract_knowledge_reply(
    state: AIClosingState,
    collected: dict[str, Any],
    text: str,
) -> str | None:
    plan = _seller_ftth_plan(state, collected)
    if not plan:
        return None

    key = _norm(text)
    # Resposta curta ("7") à pergunta "com quantos meses pretende cancelar?".
    pending_months: int | None = None
    if collected.pop("_pending_penalty_months", None):
        pending_months = _seller_bare_months_answer(text)
    asks_fidelity = any(term in key for term in (
        "fidelidade",
        "fidelizacao",
        "fidelização",
        "permanencia",
        "permanência",
        "prazo do contrato",
        "prazo de contrato",
        "tempo de contrato",
        "duracao do contrato",
        "duração do contrato",
        "quantos meses",
    ))
    asks_no_fidelity = any(term in key for term in (
        "sem fidelidade",
        "sem fidelizacao",
        "sem fidelização",
        "nao quero fidelidade",
        "não quero fidelidade",
    ))
    asks_installation_deadline = any(term in key for term in (
        "prazo de instalacao",
        "prazo da instalacao",
        "prazo de instalação",
        "prazo da instalação",
        "quanto tempo para instalar",
        "tempo de instalacao",
        "tempo de instalação",
    ))
    asks_activation_fee = any(term in key for term in (
        "taxa de instalacao",
        "taxa de instalação",
        "taxa de habilitacao",
        "taxa de habilitação",
        "valor da instalacao",
        "valor da instalação",
        "custo da instalacao",
        "custo da instalação",
    ))
    asks_cancel_penalty = any(term in key for term in (
        "multa",
        "cancelamento",
        "cancelar antes",
        "quebrar fidelidade",
        "quebra de fidelidade",
    ))
    asks_renewal = any(term in key for term in (
        "renovacao",
        "renovação",
        "renova automaticamente",
        "renovacao automatica",
        "renovação automática",
    ))
    asks_equipment = any(term in key for term in (
        "comodato",
        "equipamento",
        "roteador e meu",
        "roteador é meu",
        "roteador fica comigo",
    ))
    asks_late_fee = any(term in key for term in (
        "atraso",
        "juros",
        "multa por atraso",
        "atrasar a fatura",
        "atrasar o pagamento",
    ))

    speed = int(plan.get("download_mbps") or 0)
    fidelity_price = _seller_brl(plan.get("price_fidelity") or 0)
    no_fidelity_price = _seller_brl(plan.get("price_no_fidelity") or 0)

    if asks_no_fidelity:
        return (
            f"Sim. O plano de {speed} Mega também possui opção sem fidelidade: "
            f"R$ {no_fidelity_price} por mês, com taxa de instalação/habilitação de R$ 860,00. "
            f"Com fidelidade de 12 meses, ele fica em R$ {fidelity_price} por mês e essa taxa é integralmente isenta."
        )

    if asks_installation_deadline:
        return (
            "O prazo contratual para instalação é de até 45 dias úteis. "
            "A equipe segue com o agendamento conforme a disponibilidade técnica do endereço."
        )

    if asks_activation_fee:
        return (
            "Com fidelidade de 12 meses, a taxa de instalação/habilitação de R$ 860,00 é integralmente isenta. "
            f"Na opção sem fidelidade, a taxa de R$ 860,00 é mantida e o plano de {speed} Mega fica em R$ {no_fidelity_price} por mês."
        )

    if asks_late_fee:
        return (
            "Em caso de atraso, o contrato prevê juros de 1% ao mês, multa de 2% e correção monetária conforme o índice previsto no contrato."
        )

    if asks_cancel_penalty or pending_months is not None:
        months_used = _seller_contract_months_used(text)
        if months_used is None:
            months_used = pending_months
        if months_used is not None:
            if months_used >= 12:
                return (
                    "Após cumprir os 12 meses de fidelidade, não há multa de permanência "
                    "por cancelamento antecipado."
                )

            no_fidelity = Decimal(str(plan.get("price_no_fidelity") or 0))
            offered = Decimal(str(getattr(state, "offer_price", None) or 0))
            if offered and no_fidelity and abs(offered - no_fidelity) < Decimal("0.01"):
                return (
                    "Nesta contratação sem fidelidade não há multa de permanência "
                    "por cancelamento antecipado."
                )

            penalty, total_benefit, remaining, free_months = _seller_contract_penalty_value(
                state,
                collected,
                plan,
                months_used=months_used,
            )
            monthly = Decimal(str(
                getattr(state, "offer_price", None)
                or plan.get("price_fidelity")
                or 0
            ))

            if free_months:
                benefit_detail = (
                    f"R$ 860,00 de instalação/habilitação isenta + {free_months} "
                    f"mensalidade{'s' if free_months != 1 else ''} grátis de R$ {_seller_brl(monthly)}"
                )
            else:
                benefit_detail = "R$ 860,00 de instalação/habilitação isenta"

            return (
                f"Se você usar {months_used} meses de uma fidelidade de 12 meses, faltam {remaining} meses. "
                f"Considerando os benefícios desta contratação ({benefit_detail}), o benefício total é "
                f"R$ {_seller_brl(total_benefit)}. Pela fórmula proporcional do contrato, a multa nesse ponto "
                f"fica em R$ {_seller_brl(penalty)}."
            )

        collected["_pending_penalty_months"] = True
        return (
            "A multa de cancelamento é proporcional aos benefícios da contratação e aos meses que faltarem "
            "para completar os 12 meses. Se você me disser com quantos meses pretende cancelar, eu calculo "
            "o valor em reais para você."
        )

    if asks_renewal:
        return (
            "Depois dos 12 meses, o serviço pode continuar normalmente. "
            "Uma nova fidelidade não começa automaticamente: para existir novo período de permanência é necessário um novo termo/contrato de permanência."
        )

    if asks_equipment:
        return (
            "Os equipamentos fornecidos para o serviço são disponibilizados em comodato, ou seja, permanecem de propriedade da Sulnet durante a contratação."
        )

    if asks_fidelity:
        return (
            f"Para o plano residencial de {speed} Mega, a fidelidade/permanência é de 12 meses. "
            "Depois desse período o serviço pode continuar normalmente, e uma nova fidelidade não é criada automaticamente sem um novo termo de permanência."
        )

    return None


def _seller_existing_customer_plan_change_requested(
    db: Session,
    conversation_id: int,
    text: str,
) -> bool:
    latest = _norm(text)
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
    if not any(cue in latest for cue in change_cues):
        return False

    recent = list(
        db.scalars(
            select(Message.body)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == "INBOUND",
            )
            .order_by(Message.id.desc())
            .limit(10)
        )
    )
    combined = " | ".join(_norm(str(item or "")) for item in recent)
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
    return any(cue in combined for cue in existing_service_cues)


def _wants_other_cpf(text: str) -> bool:
    key = _norm(text)
    phrases = (
        "outro cpf",
        "outro documento",
        "cpf diferente",
        "cpf de outra pessoa",
        "cpf de outra pessoa",
        "cpf de outro titular",
        "trocar o cpf",
        "trocar cpf",
        "novo cpf",
        "tentar outro cpf",
        "contratar em outro cpf",
        "contratar com outro cpf",
    )
    return any(phrase in key for phrase in phrases)


def _clear_rejected_credit(state: AIClosingState, collected: dict[str, Any]) -> None:
    state.credit_status = ""
    state.credit_analysis_id = None
    collected.pop("creditStatus", None)
    collected.pop("creditAnalysisId", None)


def _extract_birth_date(text: str) -> str | None:
    value = text or ""
    patterns = [
        (r"\b(\d{2}/\d{2}/\d{4})\b", "%d/%m/%Y"),
        (r"\b(\d{2}-\d{2}-\d{4})\b", "%d-%m-%Y"),
        (r"\b(\d{2}\.\d{2}\.\d{4})\b", "%d.%m.%Y"),
        (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
    ]
    for pattern, fmt in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), fmt)
        except ValueError:
            continue
        if parsed > datetime.now():
            continue
        return parsed.strftime("%Y-%m-%d")
    return None


def _extract_cep(text: str) -> str | None:
    for item in re.findall(r"\d{5}-?\d{3}", text or ""):
        d = _digits(item)
        if len(d) == 8:
            return d
    digits = _digits(text)
    return digits if len(digits) == 8 else None


def _extract_email(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text or "")
    return match.group(0).lower() if match else None


def _extract_address_number(text: str) -> tuple[str | None, str]:
    value = (text or "").strip()
    match = re.search(r"\b(?:numero|n[uú]mero|n[º°o]?\.?\s*)?(\d{1,7}[A-Za-z]?)\b", value, re.I)
    number = match.group(1) if match else None
    complement = ""
    comp = re.search(
        r"\b(ap(?:to|artamento)?\.?\s*\w+|bloco\s+\w+|casa\s+\w+|sala\s+\w+|fundos|frente|lote\s+\w+)\b",
        value,
        re.I,
    )
    if comp:
        complement = comp.group(1).strip()
    elif number and "," in value:
        complement = value.split(",", 1)[1].strip()
    return number, complement


def _yes_no(text: str) -> str:
    n = _norm(text)
    if re.search(r"\b(sim|confirmo|confirmado|pode|correto|certo|isso|fechado|vamos)\b", n):
        return "SIM"
    if re.search(r"\b(nao|errado|trocar|mudar|outro|outra)\b", n):
        return "NAO"
    return "NAO_INFORMADO"


def _billing_merge(collected: dict[str, Any], text: str) -> dict[str, Any]:
    """Faturamento a partir do que o cliente já respondeu + a mensagem atual.

    A etapa BILLING é respondida em mais de uma mensagem ("10", depois
    "Boleto"). Ler só a mensagem atual fazia o Vendedor esquecer a resposta
    anterior e repetir a pergunta em ciclo (conversa 62).
    """
    billing: dict[str, Any] = {
        key: collected[key]
        for key in ("paymentMethod", "dueDay", "invoiceDelivery")
        if collected.get(key) not in (None, "")
    }
    current = _billing_local(text)
    if billing.get("paymentMethod") and not current.get("dueDay"):
        # Forma de pagamento já respondida: um número sozinho é o dia.
        bare_day = re.fullmatch(r"\s*(\d{1,2})\s*[.!]?\s*", text or "")
        if bare_day and 1 <= int(bare_day.group(1)) <= 31:
            current["dueDay"] = int(bare_day.group(1))
    billing.update(current)
    return billing


def _billing_local(text: str) -> dict[str, Any]:
    n = _norm(text)
    result: dict[str, Any] = {}
    methods = [
        ("debito em conta", "Débito em conta"),
        ("cartao", "Cartão"),
        ("boleto", "Boleto"),
        ("pix", "PIX"),
    ]
    for token, label in methods:
        if token in n:
            result["paymentMethod"] = label
            break
    day = re.search(r"\b(?:dia|vencimento(?:\s+no)?(?:\s+dia)?)\s*(\d{1,2})\b", n)
    if day:
        value = int(day.group(1))
        if 1 <= value <= 31:
            result["dueDay"] = value
    if "email" in n or "e-mail" in n:
        result["invoiceDelivery"] = "E-mail"
    elif "whatsapp" in n or "whats" in n:
        result["invoiceDelivery"] = "WhatsApp"
    return result


MOBILE_SIGNATURE_TYPE = "Assinatura Digital IA"
SDR_IA_DEFAULTS_FALLBACK = {
    "vencimento": "10",
    "forma_pagamento": "Boleto",
    "envio_fatura": ["WhatsApp"],
    "taxa_ativacao": "Não",
    "tipo_assinatura_contrato": MOBILE_SIGNATURE_TYPE,
}
SDR_IA_SKIP_CODES = {
    "novoiccid",
    "porticcid",
    "portdata",
    "m2mquantidade",
    "m2miccids",
    "unificarfaturas",
    "unificarmotivo",
    "vencimento",
    "formapagamento",
    "enviofatura",
    "taxaativacao",
    "taxavalor",
    "tipoassinaturacontrato",
}
SDR_IA_FORBIDDEN_STEPS = {
    "MOBILE_NEW_ICCID",
    "MOBILE_PORT_ICCID",
    "MOBILE_PORT_DATE",
    "MOBILE_M2M_QTY",
    "MOBILE_M2M_ICCIDS",
    "MOBILE_DUE_DAY",
    "MOBILE_PAYMENT_METHOD",
    "MOBILE_INVOICE_DELIVERY",
    "MOBILE_ACTIVATION_FEE",
    "MOBILE_ACTIVATION_VALUE",
    "MOBILE_UNIFY",
    "MOBILE_UNIFY_REASON",
    "BILLING_DUE_DAY",
    "BILLING_PAYMENT_METHOD",
    "BILLING_INVOICE_DELIVERY",
    "BILLING_ACTIVATION_FEE",
    "BILLING_ACTIVATION_VALUE",
    "BILLING_UNIFY",
    "BILLING_UNIFY_REASON",
}
MOBILE_CONTRACT_BUTTONS = [
    {"id": "portabilidade", "label": "Portabilidade"},
    {"id": "numero_novo", "label": "Número novo"},
    {"id": "m2m", "label": "M2M"},
]
MOBILE_CONTRACT_BUTTONS_SDR_IA = [
    {"id": "portabilidade", "label": "Portabilidade"},
    {"id": "numero_novo", "label": "Número novo"},
]

SELLER_STEP_BUTTONS: dict[str, list[dict[str, str]]] = {
    "COMBO_MOBILE_PLAN": [
        {"id": "movel_2", "label": "2GB"},
        {"id": "movel_5", "label": "5GB"},
        {"id": "movel_10", "label": "10GB"},
    ],
    "CONFIRM_SALE": [
        {"id": "confirmo", "label": "Confirmo"},
        {"id": "ajustar", "label": "Quero ajustar"},
    ],
    "CREDIT_REJECTED": [
        {"id": "outro_cpf", "label": "Tentar outro CPF"},
        {"id": "encerrar", "label": "Encerrar"},
    ],
    "PLAN_CONFIRM": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
    "MOBILE_CONTRACT_TYPE": MOBILE_CONTRACT_BUTTONS,
    "MOBILE_PORT_TITULAR": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
    "MOBILE_PAYMENT_METHOD": [
        {"id": "boleto", "label": "Boleto"},
        {"id": "debito", "label": "Débito em conta"},
    ],
    "MOBILE_INVOICE_DELIVERY": [
        {"id": "whatsapp", "label": "WhatsApp"},
        {"id": "email", "label": "E-mail"},
        {"id": "ambos", "label": "Ambos"},
    ],
    "MOBILE_UNIFY": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
    "MOBILE_ACTIVATION_FEE": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
    "BILLING_PAYMENT_METHOD": [
        {"id": "boleto", "label": "Boleto"},
        {"id": "debito", "label": "Débito em conta"},
    ],
    "BILLING_INVOICE_DELIVERY": [
        {"id": "whatsapp", "label": "WhatsApp"},
        {"id": "email", "label": "E-mail"},
        {"id": "ambos", "label": "Ambos"},
    ],
    "BILLING_UNIFY": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
    "BILLING_ACTIVATION_FEE": [
        {"id": "sim", "label": "Sim"},
        {"id": "nao", "label": "Não"},
    ],
}
REMOVED_MOBILE_STEPS: set[str] = set()


def _extract_generic_date(text: str) -> str | None:
    value = text or ""
    patterns = [
        (r"\b(\d{2}/\d{2}/\d{4})\b", "%d/%m/%Y"),
        (r"\b(\d{2}-\d{2}-\d{4})\b", "%d-%m-%Y"),
        (r"\b(\d{2}\.\d{2}\.\d{4})\b", "%d.%m.%Y"),
        (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
    ]
    for pattern, fmt in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d")
    return None


def _extract_money(text: str) -> float | None:
    match = re.search(r"(\d{1,6}(?:[.,]\d{1,2})?)", text or "")
    if not match:
        return None
    try:
        return round(float(match.group(1).replace(",", ".")), 2)
    except ValueError:
        return None


def _extract_due_day(text: str) -> int | None:
    match = re.search(r"\b(?:dia|vencimento\s*(?:dia)?)?\s*(\d{1,2})\b", _norm(text))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 31 else None


def _extract_mobile_contract_type(text: str) -> str | None:
    n = _norm(text)
    if re.search(r"\bm2m\b", n):
        return "M2M"
    if "port" in n:
        return "Número Portabilidade"
    if "numero_novo" in n or "numero novo" in n or "novo" in n or "nova linha" in n or "linha nova" in n:
        return "Número novo"
    return None


def _extract_iccid(text: str) -> str | None:
    for candidate in re.findall(r"\d{18,22}", _digits(text)):
        if 18 <= len(candidate) <= 22:
            return candidate
    digits = _digits(text)
    return digits if 18 <= len(digits) <= 22 else None


def _extract_iccids(text: str) -> list[str]:
    candidates = re.findall(r"(?<!\d)\d{18,22}(?!\d)", text or "")
    if not candidates:
        digits = _digits(text)
        if 18 <= len(digits) <= 22:
            candidates = [digits]
    return list(dict.fromkeys(candidates))


def _extract_mobile_number(text: str) -> str | None:
    digits = _digits(text)
    # Telefone BR: 10–11 (com DDD) ou 12–13 com 55. ICCID (18+) não entra aqui.
    return digits if 10 <= len(digits) <= 13 else None


def _looks_like_phone_answer(text: str) -> bool:
    """Resposta que é só (ou quase só) um telefone — não confunde com ICCID."""
    digits = _digits(text)
    if not (10 <= len(digits) <= 13):
        return False
    leftover = re.sub(r"[\d\s\-\(\)\.+]+", "", text or "").strip()
    return len(leftover) <= 3


def _recover_mobile_step_for_text(
    step: str,
    text: str,
    values: dict[str, Any],
    *,
    sdr_ia_funnel: bool = False,
) -> str:
    """Se o CRM rebobinou a etapa, interpreta a mensagem na etapa correta.

    Ex.: step=MOBILE_CONTRACT_TYPE com tipo já = Portabilidade e o cliente
    manda o número → trata como MOBILE_PORT_NUMBER.
    """
    contract_type = str(values.get("tipo_contratacao") or "")
    if (
        contract_type == "Número Portabilidade"
        and not values.get("numero_portado")
        and _looks_like_phone_answer(text)
        and step
        in {
            "MOBILE_CONTRACT_TYPE",
            "MOBILE_PORT_TITULAR",
            "MOBILE_PORT_ICCID",
            "MOBILE_PORT_NUMBER",
        }
    ):
        return "MOBILE_PORT_NUMBER"

    if (
        step == "MOBILE_CONTRACT_TYPE"
        and contract_type in {"Número novo", "Número Portabilidade", "M2M"}
        and not _extract_mobile_contract_type(text)
    ):
        nxt = _mobile_next_step(values, sdr_ia_funnel=sdr_ia_funnel)
        if nxt and nxt != "MOBILE_CONTRACT_TYPE":
            return nxt
    return step


def _extract_positive_int(text: str) -> int | None:
    match = re.search(r"\b(\d{1,4})\b", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _extract_invoice_delivery(text: str) -> list[str]:
    n = _norm(text)
    values: list[str] = []
    if "whatsapp" in n or "whats" in n:
        values.append("WhatsApp")
    if "email" in n or "e-mail" in n:
        values.append("E-mail")
    if any(token in n for token in ("ambos", "os dois", "duas formas")):
        values = ["WhatsApp", "E-mail"]
    return values


def _extract_mobile_payment(text: str) -> str | None:
    n = _norm(text)
    if "boleto" in n:
        return "Boleto"
    if "debito" in n:
        return "Débito em Conta"
    return None


def _sdr_ia_mode(collected: dict[str, Any]) -> bool:
    if "_sdr_ia_funnel" in collected:
        return bool(collected.get("_sdr_ia_funnel"))
    snapshot = collected.get("crmPlaybook")
    if isinstance(snapshot, dict):
        return bool(snapshot.get("sdrIaFunnel"))
    return False


def _sdr_ia_defaults(collected: dict[str, Any]) -> dict[str, Any]:
    raw = collected.get("_sdr_ia_defaults")
    defaults = dict(raw) if isinstance(raw, dict) else {}
    if _sdr_ia_mode(collected):
        for key, value in SDR_IA_DEFAULTS_FALLBACK.items():
            defaults.setdefault(key, value)
    return defaults


def _category_values(collected: dict[str, Any]) -> dict[str, Any]:
    raw = collected.get("categoryFormValues")
    values = dict(raw) if isinstance(raw, dict) else {}
    legacy_mobile = collected.get("mobileCategoryValues")
    if isinstance(legacy_mobile, dict):
        values.update(legacy_mobile)
    if _sdr_ia_mode(collected):
        values.update(_sdr_ia_defaults(collected))
    return values


def _store_category_values(
    collected: dict[str, Any],
    values: dict[str, Any],
    *,
    mobile: bool = False,
) -> None:
    normalized = dict(values)
    if _sdr_ia_mode(collected):
        normalized.update(_sdr_ia_defaults(collected))
    collected["categoryFormValues"] = normalized
    if mobile:
        collected["mobileCategoryValues"] = normalized


def _mobile_values(collected: dict[str, Any]) -> dict[str, Any]:
    values = _category_values(collected)
    _store_category_values(collected, values, mobile=True)
    return values


def _is_mobile_plan(state: AIClosingState, collected: dict[str, Any]) -> bool:
    category = _norm(str(collected.get("categoryName") or ""))
    label = _norm(str(state.plan_label or ""))
    return (
        "telefonia movel" in category
        or "sulmovel" in label
        or "telefonia movel" in label
        or " movel " in f" {label} "
    )


PLAYBOOK_STEP_BY_CODE = {
    "clientname": "NAME",
    "nome": "NAME",
    "clientcpf": "CPF",
    "cpf": "CPF",
    "document": "CPF",
    "documento": "CPF",
    "clientbirthdate": "BIRTH_DATE",
    "birthdate": "BIRTH_DATE",
    "datanascimento": "BIRTH_DATE",
    "cep": "CEP",
    "street": "ADDRESS_DETAILS",
    "rua": "ADDRESS_DETAILS",
    "neighborhood": "ADDRESS_DETAILS",
    "bairro": "ADDRESS_DETAILS",
    "addressnumber": "ADDRESS_NUMBER",
    "number": "ADDRESS_NUMBER",
    "numero": "ADDRESS_NUMBER",
    "productid": "PLAN_CONFIRM",
    "planid": "PLAN_CONFIRM",
    "produto": "PLAN_CONFIRM",
    "clientemail": "EMAIL",
    "email": "EMAIL",
    "tipocontratacao": "MOBILE_CONTRACT_TYPE",
    "novoiccid": "MOBILE_NEW_ICCID",
    "numeronovo": "MOBILE_NEW_NUMBER",
    "porticcid": "MOBILE_PORT_ICCID",
    "numeroportado": "MOBILE_PORT_NUMBER",
    "porttitular": "MOBILE_PORT_TITULAR",
    "portnometitular": "MOBILE_PORT_NAME",
    "portcpfcnpj": "MOBILE_PORT_DOC",
    "portdata": "MOBILE_PORT_DATE",
    "m2mquantidade": "MOBILE_M2M_QTY",
    "m2miccids": "MOBILE_M2M_ICCIDS",
}

PLAYBOOK_BILLING_STEPS = {
    "formapagamento": ("MOBILE_PAYMENT_METHOD", "BILLING_PAYMENT_METHOD"),
    "enviofatura": ("MOBILE_INVOICE_DELIVERY", "BILLING_INVOICE_DELIVERY"),
    "debitobanco": ("MOBILE_DEBIT_BANK", "BILLING_DEBIT_BANK"),
    "debitoagencia": ("MOBILE_DEBIT_AGENCY", "BILLING_DEBIT_AGENCY"),
    "debitoconta": ("MOBILE_DEBIT_ACCOUNT", "BILLING_DEBIT_ACCOUNT"),
    "debitotitular": ("MOBILE_DEBIT_HOLDER", "BILLING_DEBIT_HOLDER"),
    "unificarfaturas": ("MOBILE_UNIFY", "BILLING_UNIFY"),
    "unificarmotivo": ("MOBILE_UNIFY_REASON", "BILLING_UNIFY_REASON"),
    "vencimento": ("MOBILE_DUE_DAY", "BILLING_DUE_DAY"),
    "taxaativacao": ("MOBILE_ACTIVATION_FEE", "BILLING_ACTIVATION_FEE"),
    "taxavalor": ("MOBILE_ACTIVATION_VALUE", "BILLING_ACTIVATION_VALUE"),
}

PLAYBOOK_CATEGORY_FIELD_BY_CODE = {
    "tipocontratacao": "tipo_contratacao",
    "novoiccid": "novo_iccid",
    "numeronovo": "numero_novo",
    "porticcid": "port_iccid",
    "numeroportado": "numero_portado",
    "porttitular": "port_titular",
    "portnometitular": "port_nome_titular",
    "portcpfcnpj": "port_cpf_cnpj",
    "portdata": "port_data",
    "m2mquantidade": "m2m_quantidade",
    "m2miccids": "m2m_iccids",
    "vencimento": "vencimento",
    "taxaativacao": "taxa_ativacao",
    "taxavalor": "taxa_valor",
    "formapagamento": "forma_pagamento",
    "enviofatura": "envio_fatura",
    "debitobanco": "debito_banco",
    "debitoagencia": "debito_agencia",
    "debitoconta": "debito_conta",
    "debitotitular": "debito_titular",
    "unificarfaturas": "unificar_faturas",
    "unificarmotivo": "unificar_motivo",
    "tipoassinaturacontrato": "tipo_assinatura_contrato",
}


def _playbook_code_key(value: str) -> str:
    folded = _norm(value)
    compact = re.sub(r"[^a-z0-9]+", "", folded)

    # O CRM publica alguns campos customizados como cf_<nome>. Para o Vendedor
    # IA, o prefixo "cf" é apenas namespace do CRM; a semântica continua sendo
    # a mesma do campo já mapeado (ex.: cf_envio_fatura -> enviofatura).
    if compact.startswith("cf") and len(compact) > 2:
        without_prefix = compact[2:]
        known_codes = (
            set(PLAYBOOK_STEP_BY_CODE)
            | set(PLAYBOOK_BILLING_STEPS)
            | set(PLAYBOOK_CATEGORY_FIELD_BY_CODE)
            | set(SDR_IA_SKIP_CODES)
        )
        if without_prefix in known_codes:
            return without_prefix
    return compact


def _playbook_category(
    state: AIClosingState,
    collected: dict[str, Any],
) -> str:
    return "telefonia-movel" if _is_mobile_plan(state, collected) else "fibra"


def _playbook_store_snapshot(
    collected: dict[str, Any],
    result: CRMPlaybookResult,
) -> None:
    collected["crmPlaybook"] = result.as_dict()
    collected["_playbook_mode"] = "active"
    collected["_playbook_category"] = result.category
    collected["_playbook_ready_for_win"] = bool(result.ready_for_win)
    collected["_playbook_missing_codes"] = result.missing_codes
    collected["_playbook_next_question"] = result.next_question
    collected["_sdr_ia_funnel"] = bool(result.sdr_ia_funnel)
    if result.sdr_ia_funnel:
        defaults = dict(result.sdr_ia_defaults or {})
        for key, value in SDR_IA_DEFAULTS_FALLBACK.items():
            defaults.setdefault(key, value)
        collected["_sdr_ia_defaults"] = defaults
        collected["_sdr_ia_field_policy"] = dict(result.sdr_ia_field_policy or {})


def _playbook_clear_question(collected: dict[str, Any]) -> None:
    for key in (
        "_playbook_step",
        "_playbook_code",
        "_playbook_patch_key",
        "_playbook_question",
        "_playbook_options",
    ):
        collected.pop(key, None)


def _playbook_disable_for_closing(
    collected: dict[str, Any],
    *,
    reason: str,
) -> None:
    collected["_playbook_mode"] = "fallback"
    collected["_playbook_fallback_reason"] = reason[:240]
    _playbook_clear_question(collected)


def _crm_form_value(field: str, value: Any) -> Any:
    """Valor como o formulário do CRM aceita: "Número novo", "Número Portabilidade" ou "M2M"."""
    if field == "tipo_contratacao" and _norm(str(value or "")) in {"portabilidade", "numero portabilidade"}:
        return "Número Portabilidade"
    return value


def _find_nested(payload: Any, key: str, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(payload, dict):
        if payload.get(key) not in (None, ""):
            return payload[key]
        for item in payload.values():
            found = _find_nested(item, key, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _playbook_known_value(
    item: CRMPlaybookMissingItem,
    state: AIClosingState,
    collected: dict[str, Any],
) -> tuple[str, Any] | None:
    key = _playbook_code_key(item.code)
    known: dict[str, tuple[str, Any]] = {
        "clientname": ("clientName", collected.get("name")),
        "clientphone": ("clientPhone", collected.get("phone")),
        "clientemail": ("clientEmail", collected.get("email")),
        "cep": ("cep", collected.get("cep")),
        "street": ("street", collected.get("street")),
        "neighborhood": ("neighborhood", collected.get("neighborhood")),
        "city": ("city", collected.get("city")),
        "uf": ("uf", collected.get("uf")),
        "addressnumber": ("addressNumber", collected.get("number")),
        "number": ("addressNumber", collected.get("number")),
        "complement": ("complement", collected.get("complement")),
    }
    if key in known:
        field, value = known[key]
        if value not in (None, "", []):
            return field, value
        return None

    if key == "monthlyvalue" and state.offer_price is not None:
        return "monthlyValue", _money_for_api(state.offer_price)
    if key in {"productid", "planid"}:
        plan_id = collected.get("planId")
        if plan_id not in (None, "", 0, "0"):
            return "planId", plan_id
    if key == "categoryid":
        category_id = collected.get("categoryId")
        if category_id not in (None, "", 0, "0"):
            return "categoryId", category_id

    category_field = PLAYBOOK_CATEGORY_FIELD_BY_CODE.get(key)
    if category_field:
        values = _category_values(collected)
        # Portabilidade no nome do próprio cliente: titular e documento são os
        # dados dele (já consultados), não se pergunta de novo.
        if key in {"portnometitular", "portcpfcnpj"} and _norm(str(values.get("port_titular") or "")) == "sim":
            own = collected.get("name") if key == "portnometitular" else collected.get("cpf")
            if own not in (None, "", []):
                return f"category:{category_field}", own
        value = values.get(category_field)
        if value not in (None, "", []):
            return f"category:{category_field}", _crm_form_value(category_field, value)

    return None


def _playbook_dynamic_options(
    collected: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = collected.get("_playbook_dynamic_options")
    if not isinstance(raw, list):
        return []
    return [
        dict(item)
        for item in raw
        if isinstance(item, dict)
        and str(item.get("label") or "").strip()
    ]


def _playbook_current_options(
    collected: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = collected.get("_playbook_options")
    if not isinstance(raw, list):
        return []
    return [
        dict(item)
        for item in raw
        if isinstance(item, dict)
        and str(item.get("label") or "").strip()
    ]


def _canonical_playbook_value(
    item: CRMPlaybookMissingItem,
    value: Any,
) -> tuple[bool, Any]:
    """Resolve valor local para o valor interno publicado pelo CRM.

    Se o playbook publicar opções, nunca enviamos um valor inventado: procuramos
    correspondência exata/semântica única por id, label ou value. Se não houver
    correspondência única, devolvemos False para que a IA pergunte ao cliente.
    """
    if not item.options:
        return True, value

    folded = _norm(str(value or ""))
    if not folded:
        return False, None

    exact: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    for option in item.options:
        candidates = [
            _norm(str(option.get("id") or "")),
            _norm(str(option.get("label") or "")),
            _norm(str(option.get("value") or "")),
        ]
        candidates = [candidate for candidate in candidates if candidate]
        if folded in candidates:
            exact.append(option)
            continue
        if any(
            candidate in folded or folded in candidate
            for candidate in candidates
            if len(candidate) >= 4
        ):
            semantic.append(option)

    matches = exact or semantic
    unique: list[dict[str, Any]] = []
    seen_values: set[str] = set()
    for option in matches:
        marker = repr(option.get("value"))
        if marker in seen_values:
            continue
        seen_values.add(marker)
        unique.append(option)

    if len(unique) != 1:
        return False, None
    return True, unique[0].get("value")


def _current_playbook_canonical_value(
    collected: dict[str, Any],
    value: Any,
) -> Any:
    options = _playbook_current_options(collected)
    if not options:
        return value

    item = CRMPlaybookMissingItem(
        code=str(collected.get("_playbook_code") or ""),
        patch_key=str(collected.get("_playbook_patch_key") or ""),
        options=options,
    )
    matched, canonical = _canonical_playbook_value(item, value)
    return canonical if matched else value


def _playbook_recovery_metadata(
    state: AIClosingState,
    collected: dict[str, Any],
) -> dict[str, Any]:
    playbook_step = str(collected.get("_playbook_step") or "").strip()
    code = str(collected.get("_playbook_code") or "").strip()
    patch_key = str(collected.get("_playbook_patch_key") or "").strip()
    required = bool(
        playbook_step
        and playbook_step == state.step
        and code
        and patch_key
    )
    return {
        "closing_step": state.step,
        "crm_playbook_required": required,
        "crm_playbook_step": playbook_step,
        "crm_playbook_code": code,
        "crm_playbook_patch_key": patch_key,
    }


def _resolve_dynamic_playbook_value(
    text: str,
    collected: dict[str, Any],
) -> Any:
    clean = str(text or "").strip()
    folded = _norm(clean)
    for option in _playbook_dynamic_options(collected):
        candidates = {
            _norm(str(option.get("id") or "")),
            _norm(str(option.get("label") or "")),
            _norm(str(option.get("value") or "")),
        }
        if folded and folded in candidates:
            return option.get("value")
    return clean


def _sync_known_playbook_missing(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    item: CRMPlaybookMissingItem,
) -> bool:
    if not state.opportunity_id:
        return False
    if _playbook_code_key(item.code) == "portcpfcnpj" and not collected.get("cpf"):
        try:
            cpf_from_crm = _digits(str(_find_nested(client.get_opportunity(state.opportunity_id), "clientCpf") or ""))
        except CRMIntegrationError:
            cpf_from_crm = ""
        if len(cpf_from_crm) == 11:
            collected["cpf"] = cpf_from_crm
    if (
        _playbook_code_key(item.code) in {"monthlyvalue", "productid", "planid"}
        and not collected.get("planId")
        and state.plan_label
    ):
        # 2ª venda do combo: o plano veio da escolha no WhatsApp, não do
        # PLAN_CONFIRM; resolve o produto do catálogo antes de perguntar.
        try:
            _sync_plan_context(client, state, collected)
        except CRMIntegrationError:
            pass
    known = _playbook_known_value(item, state, collected)
    if not known:
        return False

    field, value = known
    matched, canonical_value = _canonical_playbook_value(item, value)
    if item.options and not matched:
        # O CRM publicou uma lista fechada e o valor legado não corresponde
        # de forma inequívoca. Não adivinhamos: a próxima etapa pergunta ao cliente.
        return False
    if matched:
        value = canonical_value

    if item.patch_key:
        client.update_playbook_field(
            state.opportunity_id,
            patch_key=item.patch_key,
            value=value,
        )
        if field.startswith("category:"):
            category_field = field.split(":", 1)[1]
            values = _category_values(collected)
            values[category_field] = value
            _store_category_values(
                collected,
                values,
                mobile=_is_mobile_plan(state, collected),
            )
        LOG.info(
            "seller_playbook_synced_patch_key opportunity=%s code=%s patch_key=%s",
            state.opportunity_id,
            item.code,
            item.patch_key,
        )
        return True

    payload: dict[str, Any]
    if field == "planId":
        payload = {
            "planId": value,
            "planLabel": collected.get("catalogPlanName") or state.plan_label,
        }
        client.update_opportunity(state.opportunity_id, payload)
    elif field == "categoryId":
        # categoryId acompanha o produto: grava junto com o plano já resolvido
        # no catálogo, nunca sozinho (o que poderia trocar o plano).
        try:
            _persist_category_form(client, state, collected)
        except CRMIntegrationError:
            return False
    elif field.startswith("category:"):
        category_field = field.split(":", 1)[1]
        values = _category_values(collected)
        values[category_field] = value
        _persist_category_form(
            client,
            state,
            collected,
            values,
        )
    else:
        client.update_opportunity(
            state.opportunity_id,
            {field: value},
        )

    LOG.info(
        "seller_playbook_synced_known opportunity=%s code=%s field=%s",
        state.opportunity_id,
        item.code,
        field,
    )
    return True


def _sdr_ia_skip_code(code: str, collected: dict[str, Any]) -> bool:
    return _sdr_ia_mode(collected) and _playbook_code_key(code) in SDR_IA_SKIP_CODES


def _sync_sdr_ia_defaults(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> bool:
    if not _sdr_ia_mode(collected):
        return False

    # A política do pacote manda aplicar os defaults ao iniciar faturamento.
    # Antes de produto/categoria existirem, apenas guardamos os defaults no estado.
    if not collected.get("planId") or not collected.get("categoryId"):
        try:
            _sync_plan_context(client, state, collected)
        except CRMIntegrationError:
            return False
    if not collected.get("planId") or not collected.get("categoryId"):
        return False

    current = _category_values(collected)
    desired = dict(current)
    desired.update(_sdr_ia_defaults(collected))
    if desired == current and collected.get("_sdr_ia_defaults_persisted"):
        return False
    _persist_category_form(
        client,
        state,
        collected,
        desired,
    )
    collected["_sdr_ia_defaults_persisted"] = True
    LOG.info(
        "seller_sdr_ia_defaults_synced opportunity=%s keys=%s",
        state.opportunity_id,
        sorted(_sdr_ia_defaults(collected).keys()),
    )
    return True


def _playbook_step_for_item(
    item: CRMPlaybookMissingItem,
    state: AIClosingState,
    collected: dict[str, Any],
) -> str | None:
    key = _playbook_code_key(item.code)
    is_mobile = _is_mobile_plan(state, collected)

    if _sdr_ia_skip_code(item.code, collected):
        return None

    billing_steps = PLAYBOOK_BILLING_STEPS.get(key)
    if billing_steps:
        return billing_steps[0] if is_mobile else billing_steps[1]

    step = PLAYBOOK_STEP_BY_CODE.get(key)
    if step and step.startswith("MOBILE_") and not is_mobile:
        return None
    return step


def _query_playbook_guidance(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    fallback_step: str,
) -> tuple[str, str | None]:
    if not state.opportunity_id:
        return fallback_step, None

    if collected.get("_playbook_mode") == "fallback":
        return fallback_step, None

    category = _playbook_category(state, collected)
    synced_codes: set[str] = set()
    for _ in range(12):
        try:
            result = client.playbook(
                state.opportunity_id,
                category=category,
            )
        except CRMIntegrationError as exc:
            if exc.status_code in {404, 405, 501}:
                _playbook_disable_for_closing(
                    collected,
                    reason=f"HTTP_{exc.status_code}",
                )
                LOG.info(
                    "seller_playbook_fallback opportunity=%s reason=http_%s",
                    state.opportunity_id,
                    exc.status_code,
                )
                return fallback_step, None
            raise

        _playbook_store_snapshot(collected, result)
        if result.credit_status:
            state.credit_status = result.credit_status

        if result.sdr_ia_funnel and _sync_sdr_ia_defaults(
            client,
            state,
            collected,
        ):
            # O PATCH automático também precisa ser seguido por novo GET /playbook.
            continue

        if result.credit_rejected:
            question = (
                result.next_question
                or "A consulta cadastral não foi aprovada automaticamente. "
                "Se quiser, posso encaminhar para análise de um atendente."
            )
            collected["_playbook_step"] = "CREDIT_REJECTED"
            collected["_playbook_question"] = question
            return "CREDIT_REJECTED", question

        if result.ready_for_win:
            _playbook_clear_question(collected)
            return fallback_step, None

        if not result.missing:
            _playbook_clear_question(collected)
            collected["_playbook_ready_for_win"] = False
            return "WAITING_CRM_READY", None

        actionable = [
            item
            for item in result.missing
            if not _sdr_ia_skip_code(item.code, collected)
        ]
        if not actionable:
            skipped = [item.code for item in result.missing]
            collected["_sdr_ia_skipped_missing_codes"] = skipped
            _playbook_clear_question(collected)
            LOG.warning(
                "seller_sdr_ia_only_skipped_fields opportunity=%s codes=%s",
                state.opportunity_id,
                skipped,
            )
            if result.sdr_ia_funnel:
                # Esses campos foram explicitamente vetados no fluxo SDR IA.
                # O endpoint contract/generate é responsável por aplicar defaults
                # comerciais homologados e validar readiness do lado do CRM.
                collected["_contract_generation_allowed"] = True
                return fallback_step, None
            return "WAITING_CRM_READY", None

        # Pode pular vários missing já coletados sem novo GET /playbook.
        selected = None
        while actionable:
            first = actionable[0]
            first_key = _playbook_code_key(first.code)
            known_locally = _playbook_known_value(first, state, collected)
            if known_locally and first_key not in synced_codes:
                synced = _sync_known_playbook_missing(
                    client,
                    state,
                    collected,
                    first,
                )
                synced_codes.add(first_key)
                if synced:
                    # Sincroniza no máximo uma vez por campo; reconsulta o CRM.
                    selected = None
                    break
                # Sync falhou (ex.: opções do CRM não batem) → pergunta ao cliente.
                selected = first
                break
            if known_locally and first_key in synced_codes:
                # CRM ainda lista o campo depois do PATCH. Não repergunta
                # Portabilidade/número: segue para o próximo missing sem valor local.
                # Bug ao vivo 04/11: após informar o número a portar, o playbook
                # voltava para tipo_contratacao e gerava looping Sim/Não.
                actionable = [
                    item
                    for item in actionable
                    if not (
                        _playbook_code_key(item.code) in synced_codes
                        and _playbook_known_value(item, state, collected)
                    )
                ]
                continue
            selected = first
            break

        if selected is None:
            if not actionable:
                LOG.warning(
                    "seller_playbook_skip_known_exhausted opportunity=%s",
                    state.opportunity_id,
                )
                _playbook_clear_question(collected)
                return fallback_step, None
            # Sync bem-sucedido: novo GET /playbook.
            continue

        first = selected
        next_step = _playbook_step_for_item(first, state, collected)
        if not next_step:
            question = (
                result.next_question
                if first is result.missing[0]
                else first.question
            ) or first.question or (
                f"Para continuar, preciso confirmar {first.label or first.code}."
            )
            collected["_playbook_dynamic_code"] = first.code
            collected["_playbook_dynamic_patch_key"] = (
                first.patch_key or first.code
            )
            collected["_playbook_dynamic_label"] = first.label
            collected["_playbook_dynamic_options"] = [
                dict(item) for item in first.options
            ]
            collected["_playbook_step"] = "PLAYBOOK_DYNAMIC_FIELD"
            collected["_playbook_code"] = first.code
            collected["_playbook_patch_key"] = first.patch_key
            collected["_playbook_question"] = question
            collected.pop("_playbook_unmapped_code", None)
            collected.pop("_playbook_unmapped_patch_key", None)
            LOG.warning(
                "seller_playbook_dynamic opportunity=%s code=%s patch_key=%s",
                state.opportunity_id,
                first.code,
                first.patch_key or first.code,
            )
            return "PLAYBOOK_DYNAMIC_FIELD", question

        # nextQuestion é a fonte de verdade quando corresponde ao primeiro
        # campo acionável. Se o CRM ainda devolver um campo vetado pelo SDR IA
        # antes dele, usamos a question do próprio item permitido.
        question = (
            result.next_question
            if first is result.missing[0]
            else first.question
        ) or first.question or None
        collected.pop("_playbook_unmapped_code", None)
        collected.pop("_playbook_unmapped_patch_key", None)
        collected["_playbook_step"] = next_step
        collected["_playbook_code"] = first.code
        collected["_playbook_patch_key"] = first.patch_key
        collected["_playbook_options"] = [
            dict(item) for item in first.options
        ]
        if question:
            collected["_playbook_question"] = question
        else:
            collected.pop("_playbook_question", None)
        return next_step, question

    LOG.warning(
        "seller_playbook_sync_limit opportunity=%s",
        state.opportunity_id,
    )
    return fallback_step, None


_MOBILE_STEP_ORDER = (
    "MOBILE_CONTRACT_TYPE",
    "MOBILE_NEW_ICCID",
    "MOBILE_NEW_NUMBER",
    "MOBILE_PORT_ICCID",
    "MOBILE_PORT_NUMBER",
    "MOBILE_PORT_TITULAR",
    "MOBILE_PORT_NAME",
    "MOBILE_PORT_DOC",
    "MOBILE_PORT_DATE",
    "MOBILE_M2M_QTY",
    "MOBILE_M2M_ICCIDS",
    "MOBILE_PAYMENT_METHOD",
    "MOBILE_INVOICE_DELIVERY",
    "MOBILE_DEBIT_BANK",
    "MOBILE_DEBIT_AGENCY",
    "MOBILE_DEBIT_ACCOUNT",
    "MOBILE_DEBIT_HOLDER",
    "MOBILE_DUE_DAY",
    "MOBILE_ACTIVATION_FEE",
    "MOBILE_ACTIVATION_VALUE",
    "MOBILE_UNIFY",
    "MOBILE_UNIFY_REASON",
)


def _mobile_step_rank(step: str) -> int:
    try:
        return _MOBILE_STEP_ORDER.index(step)
    except ValueError:
        return -1


def _playbook_should_keep_local_mobile_progress(
    *,
    playbook_step: str,
    fallback_step: str,
    collected: dict[str, Any],
) -> bool:
    """Evita o CRM rebobinar a conversa para uma etapa móvel já respondida."""
    if not fallback_step.startswith("MOBILE_") or not playbook_step.startswith("MOBILE_"):
        return False
    if playbook_step == fallback_step:
        return False
    values = _mobile_values(collected)
    local_next = _mobile_next_step(values, sdr_ia_funnel=_sdr_ia_mode(collected))
    if local_next == fallback_step:
        return _mobile_step_rank(playbook_step) < _mobile_step_rank(fallback_step)
    if local_next and _mobile_step_rank(playbook_step) < _mobile_step_rank(local_next):
        return True
    return False


def _advance_with_playbook(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    fallback_step: str,
) -> str:
    next_step, question = _query_playbook_guidance(
        client,
        state,
        collected,
        fallback_step=fallback_step,
    )
    if _playbook_should_keep_local_mobile_progress(
        playbook_step=next_step,
        fallback_step=fallback_step,
        collected=collected,
    ):
        LOG.warning(
            "seller_playbook_rewind_blocked opportunity=%s",
            state.opportunity_id,
        )
        next_step = fallback_step
        question = None
        collected["_playbook_step"] = fallback_step
        collected.pop("_playbook_question", None)
        collected.pop("_playbook_options", None)
    state.step = next_step
    if question:
        return question
    return _prompt_for_step(state, collected)


def _plan_matches_label(product: dict[str, Any], label: str) -> bool:
    """O produto do CRM corresponde ao plano escolhido na conversa?

    Em 05/11 (conversa 546) a 2ª venda do combo herdou o produto da fibra da
    1ª venda e o resumo do móvel saiu como "Internet Fibra 800 Mega". Um
    produto de outra categoria ou de outra franquia/velocidade nunca vale.
    """
    wanted = _norm(label).replace(" ", "")
    if not wanted:
        return True
    name = _norm(str(product.get("name") or "")).replace(" ", "")
    category = _norm(str(product.get("categoryName") or "")).replace(" ", "")
    product_is_mobile = "movel" in category or "movel" in name or bool(re.search(r"\d+gb", name))
    capacity = re.search(r"(\d{1,3})gb", wanted)
    wanted_is_mobile = "movel" in wanted or bool(capacity)
    wanted_is_fiber = ("fibra" in wanted or "mega" in wanted or bool(re.search(r"\d{3,4}mb", wanted))) and not wanted_is_mobile
    if wanted_is_mobile and not product_is_mobile:
        return False
    if wanted_is_fiber and product_is_mobile:
        return False
    if capacity and f"{capacity.group(1)}gb" not in name:
        return False
    speed = re.search(r"(\d{3,4})(?:mega|mb)", wanted)
    if wanted_is_fiber and speed and speed.group(1) not in name:
        return False
    return True


def _sync_plan_context(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> None:
    if not state.opportunity_id:
        return

    opportunity = client.get_opportunity(state.opportunity_id)
    raw_plan_id = opportunity.get("planId")
    if not raw_plan_id:
        raw_plan_ids = opportunity.get("planIds")
        if isinstance(raw_plan_ids, list) and raw_plan_ids:
            raw_plan_id = raw_plan_ids[0]
    try:
        plan_id = int(raw_plan_id or 0)
    except (TypeError, ValueError):
        plan_id = 0

    products = client.products("")
    selected = next(
        (item for item in products if int(item.get("id") or 0) == plan_id),
        None,
    )
    plan_corrected = False
    if selected and state.plan_label and not _plan_matches_label(selected, state.plan_label):
        LOG.warning(
            "seller_plan_mismatch opportunity=%s crm_plan_id=%s",
            state.opportunity_id,
            plan_id,
        )
        selected = None
        plan_corrected = True

    # O nome comercial usado no WhatsApp pode não ser o nome interno do ERP.
    # Ex.: "Sulmóvel 15GB" -> "Meu Trabalho Comigo 15GB".
    if not selected and state.plan_label:
        query = [
            item for item in client.products(state.plan_label)
            if _plan_matches_label(item, state.plan_label)
        ]
        exact = [
            item for item in query
            if _norm(str(item.get("name") or "")) == _norm(state.plan_label)
        ]
        if len(exact) == 1:
            selected = exact[0]
        elif len(query) == 1:
            selected = query[0]

    if not selected and state.plan_label and state.offer_price is not None:
        folded = _norm(state.plan_label).replace(" ", "")
        capacity_match = re.search(r"(\d{1,3}gb)", folded)
        capacity = capacity_match.group(1) if capacity_match else ""
        pool = products

        if capacity:
            same_capacity = [
                item for item in pool
                if capacity in _norm(str(item.get("name") or "")).replace(" ", "")
            ]
            if same_capacity:
                pool = same_capacity

        if "sulmovel" in folded or "movel" in folded:
            mobile_pool = [
                item for item in pool
                if "movel" in _norm(
                    str(item.get("categoryName") or item.get("name") or "")
                )
            ]
            if mobile_pool:
                pool = mobile_pool

        try:
            expected = round(float(state.offer_price), 2)
        except (TypeError, ValueError):
            expected = None

        if expected is not None:
            same_price: list[dict[str, Any]] = []
            for item in pool:
                try:
                    catalog_price = round(float(item.get("monthlyValue")), 2)
                except (TypeError, ValueError):
                    continue
                if abs(catalog_price - expected) < 0.01:
                    same_price.append(item)
            if len(same_price) == 1:
                selected = same_price[0]

    if not selected:
        return

    resolved_plan_id = int(selected.get("id") or 0)
    resolved_category_id = int(selected.get("categoryId") or 0)
    collected["planId"] = resolved_plan_id
    collected["categoryId"] = resolved_category_id
    collected["categoryName"] = str(selected.get("categoryName") or "")
    collected["catalogPlanName"] = str(selected.get("name") or "").strip()
    collected["contractProductName"] = collected["catalogPlanName"]

    # Para contrato, o nome usado no CRM precisa ser exatamente o nome do
    # produto do catálogo (que é o nome conciliado pelo registry/Admin → Contratos).
    # O rótulo comercial do WhatsApp pode continuar curto no estado local.
    if (
        resolved_plan_id
        and (
            plan_id != resolved_plan_id
            or collected.get("_catalog_plan_synced") != collected["catalogPlanName"]
        )
    ):
        catalog_value = selected.get("monthlyValue")
        payload: dict[str, Any] = {
            "planId": resolved_plan_id,
            "planLabel": collected["catalogPlanName"],
        }
        if plan_corrected and resolved_category_id:
            # O produto errado veio de outra categoria (fibra no lugar do móvel).
            payload["categoryId"] = resolved_category_id
        if catalog_value not in (None, ""):
            payload["catalogMonthlyValue"] = catalog_value
            if state.offer_price is None:
                payload["monthlyValue"] = catalog_value
        elif state.offer_price is not None:
            payload["monthlyValue"] = _money_for_api(state.offer_price)
        client.update_opportunity(
            state.opportunity_id,
            payload,
        )
        collected["_catalog_plan_synced"] = collected["catalogPlanName"]


def _persist_category_form(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    values: dict[str, Any] | None = None,
) -> None:
    if not state.opportunity_id:
        raise CRMIntegrationError(
            "Oportunidade do CRM não disponível para gravar o formulário da contratação."
        )

    if not collected.get("categoryId") or not collected.get("planId"):
        _sync_plan_context(client, state, collected)

    category_id = int(collected.get("categoryId") or 0)
    plan_id = int(collected.get("planId") or 0)
    if not category_id or not plan_id:
        raise CRMIntegrationError(
            "Plano/categoria ainda não foi resolvido no catálogo do CRM."
        )

    merged = _category_values(collected)
    if values:
        merged.update(values)
    if _sdr_ia_mode(collected):
        # sdrIaDefaults são dominantes em TODO PATCH de faturamento do SDR IA.
        merged.update(_sdr_ia_defaults(collected))
    _store_category_values(
        collected,
        merged,
        mobile=_is_mobile_plan(state, collected),
    )
    client.update_category_form(
        state.opportunity_id,
        merged,
        category_id=category_id,
        plan_id=plan_id,
    )


def _persist_mobile_category_form(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> None:
    values = _mobile_values(collected)
    _persist_category_form(
        client,
        state,
        collected,
        values,
    )


def _mobile_missing_fields(
    values: dict[str, Any],
    *,
    sdr_ia_funnel: bool = False,
) -> list[str]:
    required = [
        "tipo_contratacao",
        "forma_pagamento",
    ]
    if not sdr_ia_funnel:
        required += ["unificar_faturas", "tipo_assinatura_contrato"]
    contract_type = str(values.get("tipo_contratacao") or "")
    if contract_type == "Número Portabilidade":
        required += ["numero_portado", "port_titular"]
        if values.get("port_titular") == "Não":
            required += ["port_nome_titular", "port_cpf_cnpj"]
    elif contract_type == "M2M":
        if sdr_ia_funnel:
            required.append("tipo_contratacao")
        else:
            required += ["m2m_quantidade", "m2m_iccids"]
    elif contract_type != "Número novo":
        required.append("tipo_contratacao")

    if values.get("forma_pagamento") == "Boleto":
        required.append("envio_fatura")
    elif values.get("forma_pagamento") == "Débito em Conta":
        required += ["debito_banco", "debito_agencia", "debito_conta", "debito_titular"]
    if not sdr_ia_funnel and values.get("unificar_faturas") == "Não":
        required.append("unificar_motivo")

    missing: list[str] = []
    for field in required:
        value = values.get(field)
        if value in (None, "", []):
            missing.append(field)
    return list(dict.fromkeys(missing))


def _mobile_next_step(
    values: dict[str, Any],
    *,
    sdr_ia_funnel: bool = False,
) -> str | None:
    contract_type = str(values.get("tipo_contratacao") or "")
    allowed_types = {"Número novo", "Número Portabilidade"}
    if not sdr_ia_funnel:
        allowed_types.add("M2M")
    if contract_type not in allowed_types:
        return "MOBILE_CONTRACT_TYPE"

    if contract_type == "Número Portabilidade":
        for field, step in [
            ("numero_portado", "MOBILE_PORT_NUMBER"),
            ("port_titular", "MOBILE_PORT_TITULAR"),
        ]:
            if not values.get(field):
                return step
        if values.get("port_titular") == "Não":
            for field, step in [
                ("port_nome_titular", "MOBILE_PORT_NAME"),
                ("port_cpf_cnpj", "MOBILE_PORT_DOC"),
            ]:
                if not values.get(field):
                    return step

    if contract_type == "M2M" and not sdr_ia_funnel:
        if not values.get("m2m_quantidade"):
            return "MOBILE_M2M_QTY"
        if not values.get("m2m_iccids"):
            return "MOBILE_M2M_ICCIDS"

    if not values.get("forma_pagamento"):
        return "MOBILE_PAYMENT_METHOD"
    if values.get("forma_pagamento") == "Boleto":
        if not values.get("envio_fatura"):
            return "MOBILE_INVOICE_DELIVERY"
    else:
        for field, step in [
            ("debito_banco", "MOBILE_DEBIT_BANK"),
            ("debito_agencia", "MOBILE_DEBIT_AGENCY"),
            ("debito_conta", "MOBILE_DEBIT_ACCOUNT"),
            ("debito_titular", "MOBILE_DEBIT_HOLDER"),
        ]:
            if not values.get(field):
                return step
    if not sdr_ia_funnel:
        if not values.get("unificar_faturas"):
            return "MOBILE_UNIFY"
        if values.get("unificar_faturas") == "Não" and not values.get("unificar_motivo"):
            return "MOBILE_UNIFY_REASON"
    return None

def _billing_next_step(
    values: dict[str, Any],
    *,
    sdr_ia_funnel: bool = False,
) -> str | None:
    if not values.get("forma_pagamento"):
        return "BILLING_PAYMENT_METHOD"
    if values.get("forma_pagamento") == "Boleto":
        if not values.get("envio_fatura"):
            return "BILLING_INVOICE_DELIVERY"
    elif values.get("forma_pagamento") == "Débito em Conta":
        for field, step in [
            ("debito_banco", "BILLING_DEBIT_BANK"),
            ("debito_agencia", "BILLING_DEBIT_AGENCY"),
            ("debito_conta", "BILLING_DEBIT_ACCOUNT"),
            ("debito_titular", "BILLING_DEBIT_HOLDER"),
        ]:
            if not values.get(field):
                return step
    if not sdr_ia_funnel:
        if not values.get("unificar_faturas"):
            return "BILLING_UNIFY"
        if values.get("unificar_faturas") == "Não" and not values.get("unificar_motivo"):
            return "BILLING_UNIFY_REASON"
    return None


def _apply_mobile_answer(
    step: str,
    text: str,
    values: dict[str, Any],
    *,
    sdr_ia_funnel: bool = False,
) -> tuple[bool, str]:
    if step == "MOBILE_CONTRACT_TYPE":
        contract_type = _extract_mobile_contract_type(text)
        if not contract_type or (sdr_ia_funnel and contract_type == "M2M"):
            if sdr_ia_funnel:
                return False, "Escolha uma das opções: Portabilidade ou Número novo."
            return False, "Escolha uma das opções: Portabilidade, Número novo ou M2M."
        previous = str(values.get("tipo_contratacao") or "")
        values["tipo_contratacao"] = contract_type
        if previous and previous != contract_type:
            for key in (
                "novo_iccid", "numero_novo", "port_iccid", "numero_portado",
                "port_titular", "port_nome_titular", "port_cpf_cnpj", "port_data",
                "m2m_quantidade", "m2m_iccids",
            ):
                values.pop(key, None)
        return True, ""

    if step == "MOBILE_NEW_ICCID":
        iccid = _extract_iccid(text)
        if not iccid:
            return False, "Informe o ICCID completo do chip, com 18 a 22 dígitos."
        values["novo_iccid"] = iccid
        return True, ""

    if step == "MOBILE_NEW_NUMBER":
        number = _extract_mobile_number(text)
        if not number:
            return False, "Informe o número completo com DDD."
        values["numero_novo"] = number
        return True, ""

    if step == "MOBILE_PORT_ICCID":
        iccid = _extract_iccid(text)
        if not iccid:
            return False, "Informe o ICCID completo do chip atual, com 18 a 22 dígitos."
        values["port_iccid"] = iccid
        return True, ""

    if step == "MOBILE_PORT_NUMBER":
        number = _extract_mobile_number(text)
        if not number:
            return False, "Preciso do número completo com DDD que será portado."
        values["numero_portado"] = number
        return True, ""

    if step == "MOBILE_PORT_TITULAR":
        answer = _yes_no(text)
        if answer == "NAO_INFORMADO":
            return False, "Responda sim ou não: a portabilidade está em nome do titular desta contratação?"
        values["port_titular"] = "Sim" if answer == "SIM" else "Não"
        return True, ""

    if step == "MOBILE_PORT_NAME":
        name = re.sub(r"\s+", " ", (text or "").strip())
        if len(name) < 5:
            return False, "Informe o nome completo do titular da linha."
        values["port_nome_titular"] = name[:160]
        return True, ""

    if step == "MOBILE_PORT_DOC":
        document = _extract_cpf(text)
        if not document:
            return False, "Informe o CPF ou CNPJ completo do titular da linha."
        values["port_cpf_cnpj"] = document
        return True, ""

    if step == "MOBILE_PORT_DATE":
        port_date = _extract_generic_date(text)
        if not port_date:
            return False, "Informe a data da portabilidade no formato DD/MM/AAAA."
        values["port_data"] = port_date
        return True, ""

    if step == "MOBILE_M2M_QTY":
        quantity = _extract_positive_int(text)
        if not quantity:
            return False, "Informe a quantidade de chips M2M."
        values["m2m_quantidade"] = quantity
        values.pop("m2m_iccids", None)
        return True, ""

    if step == "MOBILE_M2M_ICCIDS":
        iccids = _extract_iccids(text)
        quantity = int(values.get("m2m_quantidade") or 0)
        if not iccids:
            return False, "Informe os ICCIDs dos chips M2M, separados por espaço ou vírgula."
        if quantity and len(iccids) != quantity:
            return False, (
                f"Preciso de {quantity} ICCID(s) e recebi {len(iccids)}. "
                "Envie todos os ICCIDs na mesma mensagem."
            )
        values["m2m_iccids"] = iccids
        return True, ""

    if step == "MOBILE_DUE_DAY":
        due_day = _extract_due_day(text)
        if not due_day:
            return False, "Informe o dia de vencimento, entre 1 e 31."
        values["vencimento"] = due_day
        return True, ""

    if step == "MOBILE_ACTIVATION_FEE":
        answer = _yes_no(text)
        if answer == "NAO_INFORMADO":
            return False, "Responda sim ou não sobre a taxa de ativação."
        values["taxa_ativacao"] = "Sim" if answer == "SIM" else "Não"
        if answer == "NAO":
            values.pop("taxa_valor", None)
        return True, ""

    if step == "MOBILE_ACTIVATION_VALUE":
        amount = _extract_money(text)
        if amount is None:
            return False, "Informe o valor da taxa de ativação."
        values["taxa_valor"] = amount
        return True, ""

    if step == "MOBILE_PAYMENT_METHOD":
        method = _extract_mobile_payment(text)
        if not method:
            return False, "Escolha boleto ou débito em conta."
        values["forma_pagamento"] = method
        if method == "Boleto":
            for key in ("debito_banco", "debito_agencia", "debito_conta", "debito_titular"):
                values.pop(key, None)
        else:
            values.pop("envio_fatura", None)
        return True, ""

    if step == "MOBILE_INVOICE_DELIVERY":
        delivery = _extract_invoice_delivery(text)
        if not delivery:
            return False, "Escolha WhatsApp, e-mail ou ambos para receber a fatura."
        values["envio_fatura"] = delivery
        return True, ""

    text_value = re.sub(r"\s+", " ", (text or "").strip())
    if step in {"MOBILE_DEBIT_BANK", "MOBILE_DEBIT_AGENCY", "MOBILE_DEBIT_ACCOUNT", "MOBILE_DEBIT_HOLDER"}:
        if not text_value:
            return False, "Preciso dessa informação para configurar o débito em conta."
        field = {
            "MOBILE_DEBIT_BANK": "debito_banco",
            "MOBILE_DEBIT_AGENCY": "debito_agencia",
            "MOBILE_DEBIT_ACCOUNT": "debito_conta",
            "MOBILE_DEBIT_HOLDER": "debito_titular",
        }[step]
        values[field] = text_value[:160]
        return True, ""

    if step == "MOBILE_UNIFY":
        answer = _yes_no(text)
        if answer == "NAO_INFORMADO":
            return False, "Responda sim ou não para unificar as faturas."
        values["unificar_faturas"] = "Sim" if answer == "SIM" else "Não"
        if answer == "SIM":
            values.pop("unificar_motivo", None)
        return True, ""

    if step == "MOBILE_UNIFY_REASON":
        if len(text_value) < 3:
            return False, "Informe o motivo para não unificar as faturas."
        values["unificar_motivo"] = text_value[:300]
        return True, ""

    return False, "Não consegui interpretar esta etapa da contratação móvel."


def _apply_billing_form_answer(
    step: str,
    text: str,
    values: dict[str, Any],
) -> tuple[bool, str]:
    if step == "BILLING_PAYMENT_METHOD":
        method = _extract_mobile_payment(text)
        if not method:
            return False, "Escolha boleto ou débito em conta."
        values["forma_pagamento"] = method
        if method == "Boleto":
            for key in ("debito_banco", "debito_agencia", "debito_conta", "debito_titular"):
                values.pop(key, None)
        else:
            values.pop("envio_fatura", None)
        return True, ""

    if step == "BILLING_INVOICE_DELIVERY":
        delivery = _extract_invoice_delivery(text)
        if not delivery:
            return False, "Escolha WhatsApp, e-mail ou ambos para receber a fatura."
        values["envio_fatura"] = delivery
        return True, ""

    if step == "BILLING_DUE_DAY":
        due_day = _extract_due_day(text)
        if not due_day:
            return False, "Informe o dia de vencimento, entre 1 e 31."
        values["vencimento"] = due_day
        return True, ""

    if step == "BILLING_ACTIVATION_FEE":
        answer = _yes_no(text)
        if answer == "NAO_INFORMADO":
            return False, "Responda sim ou não sobre a taxa de ativação."
        values["taxa_ativacao"] = "Sim" if answer == "SIM" else "Não"
        if answer == "NAO":
            values.pop("taxa_valor", None)
        return True, ""

    if step == "BILLING_ACTIVATION_VALUE":
        amount = _extract_money(text)
        if amount is None:
            return False, "Informe o valor da taxa de ativação."
        values["taxa_valor"] = amount
        return True, ""

    text_value = re.sub(r"\s+", " ", (text or "").strip())
    if step in {
        "BILLING_DEBIT_BANK",
        "BILLING_DEBIT_AGENCY",
        "BILLING_DEBIT_ACCOUNT",
        "BILLING_DEBIT_HOLDER",
    }:
        if not text_value:
            return False, "Preciso dessa informação para configurar o débito em conta."
        field = {
            "BILLING_DEBIT_BANK": "debito_banco",
            "BILLING_DEBIT_AGENCY": "debito_agencia",
            "BILLING_DEBIT_ACCOUNT": "debito_conta",
            "BILLING_DEBIT_HOLDER": "debito_titular",
        }[step]
        values[field] = text_value[:160]
        return True, ""

    if step == "BILLING_UNIFY":
        answer = _yes_no(text)
        if answer == "NAO_INFORMADO":
            return False, "Responda sim ou não para unificar as faturas."
        values["unificar_faturas"] = "Sim" if answer == "SIM" else "Não"
        if answer == "SIM":
            values.pop("unificar_motivo", None)
        return True, ""

    if step == "BILLING_UNIFY_REASON":
        if len(text_value) < 3:
            return False, "Informe o motivo para não unificar as faturas."
        values["unificar_motivo"] = text_value[:300]
        return True, ""

    return False, "Não consegui interpretar esta informação de faturamento."


SIGNED_CONTRACT_STATUSES = {
    "assinado",
    "signed",
    "contrato_assinado",
    "assinatura_concluida",
    "signature_completed",
}


def _contract_is_signed(status: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", _norm(status)).strip("_")
    return normalized in SIGNED_CONTRACT_STATUSES


def _contract_sign_url(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in (
        "signUrl",
        "signatureUrl",
        "sign_url",
        "signature_url",
        "urlAssinatura",
        "url_assinatura",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested = payload.get("contract")
    if isinstance(nested, dict):
        return _contract_sign_url(nested)
    return ""


_SIGNED_VALUE = re.compile(
    r"(?<![a-z])(?:signed|assinad[oa]|contrato_assinado|assinatura_concluida|signature_completed)(?![a-z])"
)
_SIGNED_KEY = re.compile(r"(?:signed_?at|assinad[oa]_?em|signature_?completed_?at|data_?assinatura)$")


def _signed_paths(payload: Any, path: str = "", depth: int = 0) -> list[str]:
    """Caminhos do payload do CRM que indicam contrato assinado.

    Só olha campos de contrato/assinatura (status ou data de assinatura), sem
    registrar valores pessoais.
    """
    found: list[str] = []
    if depth > 5:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = re.sub(r"[^a-z0-9]+", "_", _norm(str(key))).strip("_")
            here = f"{path}.{key_text}" if path else key_text
            contract_scope = any(word in here for word in ("contr", "sign", "assin", "zapsign", "document"))
            if isinstance(value, (dict, list)):
                found.extend(_signed_paths(value, here, depth + 1))
            elif contract_scope and _SIGNED_KEY.search(key_text) and value:
                found.append(here)
            elif (
                contract_scope
                and key_text.endswith("status")
                and isinstance(value, str)
                and _SIGNED_VALUE.search(re.sub(r"[^a-z0-9]+", "_", _norm(value)))
                and not re.search(r"(?:un|not_|nao_)signed|nao_assinad", _norm(value).replace(" ", "_"))
            ):
                found.append(here)
    elif isinstance(payload, list):
        for index, item in enumerate(payload[:20]):
            found.extend(_signed_paths(item, f"{path}[{index}]", depth + 1))
    return found


def _payload_shape(payload: Any, depth: int = 0) -> str:
    """Estrutura (chaves e status) do payload, sem valores pessoais, para diagnóstico."""
    if depth > 3:
        return "…"
    if isinstance(payload, dict):
        parts = []
        for key, value in list(payload.items())[:25]:
            if isinstance(value, (dict, list)):
                parts.append(f"{key}{{{_payload_shape(value, depth + 1)}}}")
            elif "status" in str(key).lower() or "signed" in str(key).lower():
                parts.append(f"{key}={str(value)[:25]}")
            else:
                parts.append(str(key))
        return ",".join(parts)
    if isinstance(payload, list):
        return f"[{len(payload)}]" + (_payload_shape(payload[0], depth + 1) if payload else "")
    return ""


def _contract_status(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "contractStatus", "contract_status"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested = payload.get("contract")
    if isinstance(nested, dict):
        return _contract_status(nested)
    return ""


def _win_idempotency_key(state: AIClosingState) -> str:
    seed = f"sulnet-prospecta:won:{state.external_id}:{state.opportunity_id or 0}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _contract_pending_message(exc: CRMIntegrationError) -> str:
    parts: list[str] = [str(exc or "")]
    body = exc.response_body
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            value = body.get(key)
            if value not in (None, ""):
                parts.append(str(value))
    return _norm(" ".join(parts))


def _infer_mobile_contract_type(collected: dict[str, Any]) -> str | None:
    """Recupera tipo de contratação a partir do que já foi coletado na conversa."""
    values = _mobile_values(collected)
    current = _crm_form_value("tipo_contratacao", values.get("tipo_contratacao"))
    if current in {"Número novo", "Número Portabilidade", "M2M"}:
        return str(current)
    if values.get("numero_portado") or values.get("port_titular"):
        return "Número Portabilidade"
    if values.get("numero_novo"):
        return "Número novo"
    return None


def _recover_contract_pending_field(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    exc: CRMIntegrationError,
) -> str | None:
    """Converte pendência do contract/generate em etapa conversacional segura.

    O CRM é a fonte de verdade. Quando o gerador de contrato rejeita
    tipocontratacao, primeiro tentamos regravar o valor já conhecido
    (Portabilidade/Número novo) sem rebobinar a conversa. Só perguntamos de
    novo se não houver evidência local confiável.
    """
    pending = _contract_pending_message(exc)
    if exc.status_code != 400:
        return None

    if "tipo de contratacao movel" not in pending:
        return None

    if not state.opportunity_id:
        return None

    inferred = _infer_mobile_contract_type(collected)
    patch_key = "productInfo.categoryForm.values.tipo_contratacao"
    already_synced = (
        collected.get("_contract_recovery_reason") == "mobile_contract_type_synced"
    )
    if inferred and not already_synced:
        values = _mobile_values(collected)
        values["tipo_contratacao"] = inferred
        _store_category_values(collected, values, mobile=True)
        try:
            client.update_playbook_field(
                state.opportunity_id,
                patch_key=patch_key,
                value=inferred,
            )
            _persist_mobile_category_form(client, state, collected)
        except CRMIntegrationError as sync_exc:
            LOG.warning(
                "seller_contract_pending_tipo_sync_failed opportunity=%s error=%s",
                state.opportunity_id,
                str(sync_exc)[:200],
            )
        else:
            collected["_contract_generation_requested"] = False
            collected.pop("_contract_generation_response", None)
            collected["_contract_generation_allowed"] = True
            collected["_contract_recovery_reason"] = "mobile_contract_type_synced"
            collected["_playbook_code"] = "cf_tipo_contratacao"
            collected["_playbook_patch_key"] = patch_key
            LOG.warning(
                "seller_contract_pending_tipo_resynced opportunity=%s tipo=%s",
                state.opportunity_id,
                inferred,
            )
            # Retorna None para o chamador tentar gerar o contrato de novo com o
            # campo já sincronizado, em vez de reperguntar Portabilidade.
            return None

    result = client.playbook(
        state.opportunity_id,
        category="telefonia-movel",
    )
    item = next(
        (
            current
            for current in result.missing
            if _playbook_code_key(current.code) == "tipocontratacao"
        ),
        None,
    )

    question = (
        (item.question if item else "")
        or result.next_question
        or "Como você quer contratar sua linha móvel?"
    )

    collected["_contract_generation_requested"] = False
    collected.pop("_contract_generation_response", None)
    collected["_contract_generation_allowed"] = False
    collected["_contract_recovery_reason"] = "mobile_contract_type"
    collected["_playbook_step"] = "MOBILE_CONTRACT_TYPE"
    collected["_playbook_code"] = item.code if item else "cf_tipo_contratacao"
    collected["_playbook_patch_key"] = (
        item.patch_key
        if item and item.patch_key
        else patch_key
    )
    collected["_playbook_question"] = question
    collected["_playbook_options"] = [
        dict(option)
        for option in (item.options if item else [])
    ]

    # Não apagar evidência local de portabilidade/número novo — só pedimos a
    # confirmação do tipo quando realmente não há valor para sincronizar.
    state.step = "MOBILE_CONTRACT_TYPE"
    state.contract_status = "aguardando_tipo_contratacao"

    LOG.warning(
        "seller_contract_pending_recovered opportunity=%s field=tipo_contratacao "
        "options=%s",
        state.opportunity_id,
        len(item.options) if item else 0,
    )
    return question


def _prepare_contract_before_win(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> str:
    if not state.opportunity_id:
        raise CRMIntegrationError("Oportunidade do CRM não disponível para gerar contrato.")

    # A validação definitiva de readiness pertence ao CRM. Em SDR IA, o
    # playbook pode retornar campos intencionalmente vetados (ex.: port_data)
    # mesmo quando contract/generate consegue aplicar os defaults homologados.
    can_attempt_contract = bool(
        collected.get("_playbook_ready_for_win") is True
        or collected.get("_contract_generation_allowed") is True
    )
    if not can_attempt_contract:
        state.step = "WAITING_CRM_READY"
        state.contract_status = "aguardando_validacao_crm"
        return _prompt_for_step(state, collected)

    _sync_plan_context(client, state, collected)
    contract_product_name = str(collected.get("contractProductName") or "").strip()
    if not contract_product_name:
        raise CRMIntegrationError(
            "Produto do CRM não resolvido pelo catálogo para geração do contrato.",
            error_code="CRM_CONTRACT_PRODUCT_NAME_MISSING",
        )
    collected["_contract_product_name"] = contract_product_name

    state.step = "WAITING_CONTRACT"
    generated: dict[str, Any] = {}

    if not collected.get("_contract_generation_requested"):
        try:
            generated = client.generate_agent_contract(
                state.opportunity_id,
                send_signature=True,
                require_ready_for_win=True,
            )
        except CRMIntegrationError as exc:
            recovered = _recover_contract_pending_field(
                client,
                state,
                collected,
                exc,
            )
            if recovered is not None:
                return recovered
            # Recovery sincronizou tipocontratacao no CRM: tenta gerar de novo
            # uma única vez sem reperguntar Portabilidade ao cliente.
            if collected.get("_contract_recovery_reason") == "mobile_contract_type_synced":
                try:
                    generated = client.generate_agent_contract(
                        state.opportunity_id,
                        send_signature=True,
                        require_ready_for_win=True,
                    )
                except CRMIntegrationError as retry_exc:
                    recovered_retry = _recover_contract_pending_field(
                        client,
                        state,
                        collected,
                        retry_exc,
                    )
                    if recovered_retry is not None:
                        return recovered_retry
                    if retry_exc.status_code not in {404, 405, 501}:
                        raise
                    generated = {}
                    collected["_contract_generation_endpoint_unavailable"] = True
                else:
                    collected["_contract_generation_requested"] = True
                    collected["_contract_generation_response"] = {
                        key: generated.get(key)
                        for key in (
                            "protocol",
                            "templateId",
                            "status",
                            "signatureReused",
                        )
                        if generated.get(key) not in (None, "")
                    }
            elif exc.status_code not in {404, 405, 501}:
                raise
            else:
                generated = {}
                collected["_contract_generation_endpoint_unavailable"] = True
        else:
            collected["_contract_generation_requested"] = True
            collected["_contract_generation_response"] = {
                key: generated.get(key)
                for key in (
                    "protocol",
                    "templateId",
                    "status",
                    "signatureReused",
                )
                if generated.get(key) not in (None, "")
            }

    if not generated and client.supports("contract"):
        if not collected.get("_contract_start_requested"):
            generated = client.start_contract(state.opportunity_id)
            collected["_contract_start_requested"] = True

    if (
        not generated
        and collected.get("_contract_generation_endpoint_unavailable")
        and not client.supports("contract")
    ):
        state.contract_status = "aguardando_capability_contrato"
        return _prompt_for_step(state, collected)

    sign_url = _contract_sign_url(generated)
    contract_status = _contract_status(generated)
    if contract_status:
        state.contract_status = contract_status
    elif not state.contract_status:
        state.contract_status = "contrato_em_preparacao"

    # Alguns CRMs separam "generate" de "start/sign". Se a geração não trouxe
    # o link e a capability de contrato existe, iniciamos a etapa de assinatura.
    if not sign_url and client.supports("contract") and not collected.get("_contract_start_requested"):
        started = client.start_contract(state.opportunity_id)
        collected["_contract_start_requested"] = True
        sign_url = _contract_sign_url(started)
        started_status = _contract_status(started)
        if started_status:
            state.contract_status = started_status

    if sign_url:
        state.sign_url = sign_url
        if not state.contract_status:
            state.contract_status = "aguardando_assinatura"
        state.step = "WAITING_SIGNATURE"
        return (
            "Seu cadastro foi concluído e o contrato está pronto para assinatura. "
            f"Você pode assinar por este link: {sign_url} "
            "Após a assinatura, a ativação ocorre em até 48 horas."
        )

    return _prompt_for_step(state, collected)


def _opportunity_status(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    opportunity = payload.get("opportunity")
    if isinstance(opportunity, dict):
        value = str(opportunity.get("status") or "").strip().casefold()
        if value:
            return value
    return str(payload.get("status") or "").strip().casefold()


def _backoffice_id(payload: dict[str, Any] | None) -> Any:
    if not isinstance(payload, dict):
        return None
    direct = payload.get("backofficeOpportunityId")
    if direct not in (None, ""):
        return direct
    opportunity = payload.get("opportunity")
    if isinstance(opportunity, dict):
        copy = opportunity.get("backofficeCopy")
        if isinstance(copy, dict) and copy.get("id") not in (None, ""):
            return copy.get("id")
    return None


def _complete_if_crm_already_won(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    status_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not state.opportunity_id:
        return None
    if isinstance(status_payload, dict):
        status = status_payload
    else:
        try:
            status = client.get_status(state.opportunity_id)
        except CRMIntegrationError as exc:
            if exc.status_code != 404:
                raise
            status = {}
    if _opportunity_status(status) != "won":
        return None

    collected["_won_after_signature"] = True
    collected["_won_confirmed_by_crm"] = True
    backoffice_id = _backoffice_id(status)
    if backoffice_id not in (None, ""):
        collected["_backoffice_opportunity_id"] = backoffice_id
    state.step = "COMPLETED"
    state.completed_at = utcnow()
    return status


def _finalize_signed_sale(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> dict[str, Any]:
    if not state.opportunity_id:
        raise CRMIntegrationError("Oportunidade do CRM não disponível para concluir a venda.")

    already_won = _complete_if_crm_already_won(
        client,
        state,
        collected,
    )
    if already_won is not None:
        return already_won

    if not _contract_is_signed(state.contract_status):
        raise CRMIntegrationError(
            "A venda não pode ser marcada como ganha antes da assinatura do contrato.",
            error_code="CRM_CONTRACT_NOT_SIGNED",
        )

    # No endpoint agent contract/generate, o CRM/ZapSign é a autoridade da
    # transição assinatura -> won -> BKO. Pode existir uma janela curta em que
    # o contrato já aparece assinado, mas o webhook ainda está concluindo o
    # ganho. Nesse fluxo esperamos o CRM em vez de disparar um segundo mark_won.
    if collected.get("_contract_generation_requested"):
        collected["_awaiting_crm_auto_win"] = True
        state.step = "WAITING_SIGNATURE"
        return {
            "pendingAutoWin": True,
            "opportunity": {
                "id": state.opportunity_id,
                "status": "open",
            },
            "contract": {
                "status": state.contract_status,
            },
        }

    idempotency_key = _win_idempotency_key(state)
    collected["_win_idempotency_key"] = idempotency_key
    won = client.mark_won(
        state.opportunity_id,
        idempotency_key=idempotency_key,
    )
    opportunity = won.get("opportunity") if isinstance(won, dict) else {}
    status = str((opportunity or {}).get("status") or "").strip().lower()
    already_won = bool(won.get("alreadyWon")) if isinstance(won, dict) else False
    if status != "won" and not already_won:
        raise CRMIntegrationError(
            "CRM não confirmou a oportunidade como ganha após a assinatura.",
            error_code="CRM_WIN_NOT_CONFIRMED",
        )

    collected["_won_after_signature"] = True
    backoffice_id = won.get("backofficeOpportunityId") if isinstance(won, dict) else None
    if backoffice_id not in (None, ""):
        collected["_backoffice_opportunity_id"] = backoffice_id
    state.step = "COMPLETED"
    state.completed_at = utcnow()
    return won


SALE_ACCEPTANCE_STEP = "CONFIRM_SALE"
SIGNED_FINAL_MESSAGE = (
    "Contrato assinado com sucesso! ✅ Sua contratação está concluída. "
    "Agora é só aguardar: nossa equipe vai entrar em contato para agendar a "
    "ativação do seu serviço. Obrigado por escolher a Sulnet!"
)


def _friendly_plan_name(raw: str) -> str:
    """Nome do plano para o cliente, sem o código interno do CRM.

    "Sulnet RESID FIBRA - 800MB - 2-25" vira "Internet Fibra 800 Mega".
    """
    text = (raw or "").strip()
    if not text:
        return ""
    folded = _norm(text)
    speed = re.search(r"(\d{2,4})\s*(?:mb|mega)", folded)
    if speed and ("fibra" in folded or "ftth" in folded or "internet" in folded):
        return f"Internet Fibra {speed.group(1)} Mega"
    data = re.search(r"(\d{1,3})\s*gb", folded)
    if data and ("movel" in folded or "sulmovel" in folded):
        return f"Sulmóvel {data.group(1)}GB"
    return text


def _mask_cpf(value: str) -> str:
    digits = _digits(value)
    if len(digits) != 11:
        return value
    return f"{digits[:3]}.***.***-{digits[-2:]}"


def _acceptance_customer_lines(collected: dict[str, Any], form: dict[str, Any]) -> list[str]:
    """Dados do cliente, endereço e linha móvel no resumo para o aceite."""
    lines: list[str] = []
    data: list[str] = []
    if collected.get("name"):
        data.append(f"• Nome: {collected['name']}")
    if collected.get("cpf"):
        data.append(f"• CPF: {_mask_cpf(str(collected['cpf']))}")
    if collected.get("phone"):
        data.append(f"• Telefone: {collected['phone']}")
    if collected.get("email"):
        data.append(f"• E-mail: {collected['email']}")
    street = " ".join(
        part for part in (str(collected.get("street") or "").strip(), str(collected.get("number") or "").strip()) if part
    )
    if collected.get("complement"):
        street = f"{street}, {collected['complement']}".strip(", ")
    place = "/".join(part for part in (str(collected.get("city") or "").strip(), str(collected.get("uf") or "").strip()) if part)
    address = " - ".join(
        part for part in (street, str(collected.get("neighborhood") or "").strip(), place, str(collected.get("cep") or "").strip()) if part
    )
    if address:
        data.append(f"• Endereço: {address}")
    if data:
        lines += ["", "Seus dados:"] + data

    kind = str(form.get("tipo_contratacao") or "").strip()
    if kind:
        mobile = [f"• Tipo: {'Portabilidade' if 'port' in _norm(kind) else kind}"]
        if form.get("numero_portado"):
            mobile.append(f"• Número a portar: {form['numero_portado']}")
        if form.get("numero_novo"):
            mobile.append(f"• Número novo: {form['numero_novo']}")
        if "port" in _norm(kind):
            if _norm(str(form.get("port_titular") or "")) == "sim":
                mobile.append("• Titular da linha: você mesmo")
            elif form.get("port_nome_titular"):
                holder = str(form["port_nome_titular"])
                if form.get("port_cpf_cnpj"):
                    holder += f" ({_mask_cpf(str(form['port_cpf_cnpj']))})"
                mobile.append(f"• Titular da linha: {holder}")
        lines += ["", "Linha móvel:"] + mobile
    return lines


def _sale_acceptance_prompt(
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    reminder: bool = False,
) -> str:
    plan = _friendly_plan_name(
        str(collected.get("catalogPlanName") or "").strip()
        or str(state.plan_label or "").strip()
    ) or "plano escolhido"
    price = _money(state.offer_price)
    if price is None:
        price = _money(collected.get("catalogMonthlyValue"))
    price_text = f" por R$ {_seller_brl(price)}/mês" if price is not None else ""
    if reminder:
        return (
            f"Só preciso da sua confirmação para gerar o contrato: confirma a "
            f"contratação do {plan}{price_text}? Responda Sim ou Quero ajustar."
        )
    lines = [
        "Antes de gerar o contrato, confira o resumo da sua contratação:",
        f"• Plano: {plan}",
    ]
    if price is not None:
        lines.append(f"• Mensalidade: R$ {_seller_brl(price)}")
    form = _category_values(collected)
    payment = collected.get("paymentMethod") or form.get("forma_pagamento")
    due_day = collected.get("dueDay") or form.get("vencimento")
    if black_campaign.has_marker(getattr(state, "handoff_summary", "")):
        lines.append(f"• Promoção Black: 1ª mensalidade em {black_campaign.FIRST_DUE_DATE}")
    if payment:
        lines.append(f"• Pagamento: {payment}")
    if due_day:
        lines.append(f"• Vencimento: dia {due_day}")
    lines.extend(_acceptance_customer_lines(collected, form))
    lines.append("")
    lines.append(
        "Você confirma a contratação? Com o seu Sim, eu gero o contrato para "
        "assinatura digital."
    )
    return "\n".join(lines)


def _sale_acceptance_answer(text: str) -> str:
    n = _norm(text)
    if re.search(r"\b(ajust\w*|alterar|corrigir|desist\w*|cancel\w*|pensar)\b", n):
        return "NAO"
    if re.search(r"\b(confirmo|aceito|pode gerar|pode mandar|pode enviar|quero contratar)\b", n):
        return "SIM"
    return _yes_no(text)


def _finish_sale_contract(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> str:
    # O contrato só é gerado depois de um aceite explícito do resumo
    # (conversa 114: o cliente recebeu o link e respondeu "eu nem contratei nada").
    if not collected.get("_sale_accepted"):
        state.step = SALE_ACCEPTANCE_STEP
        return _sale_acceptance_prompt(state, collected)
    if state.opportunity_id:
        # O cliente só aceita o plano que viu no resumo: se o produto do CRM
        # foi corrigido depois (conversa 546), mostra o resumo certo de novo.
        shown = str(collected.get("catalogPlanName") or "").strip()
        _sync_plan_context(client, state, collected)
        current = str(collected.get("catalogPlanName") or "").strip()
        if shown and current and shown != current:
            collected["_sale_accepted"] = False
            state.step = SALE_ACCEPTANCE_STEP
            return _sale_acceptance_prompt(state, collected)
    return _prepare_contract_before_win(client, state, collected)



def _seller_knowledge(db: Session, agent: AIAgentProfile, query: str = "") -> str:
    items = list(
        db.scalars(
            select(AIKnowledgeItem).where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.active.is_(True),
            ).order_by(AIKnowledgeItem.updated_at.desc()).limit(80)
        )
    )
    # Rascunhos (REVIEW/DRAFT) só valem depois de aprovados pelo gestor.
    pending_ids = set(
        db.scalars(
            select(AIKnowledgeMetadata.knowledge_item_id).where(
                AIKnowledgeMetadata.knowledge_item_id.in_([item.id for item in items] or [0]),
                AIKnowledgeMetadata.approval_status != "APPROVED",
            )
        )
    )
    items = [item for item in items if item.id not in pending_ids]
    terms = {
        token for token in re.findall(r"[a-z0-9]{3,}", _norm(query))
        if token not in _SELLER_KB_STOPWORDS
    }
    if terms:
        def score(item: AIKnowledgeItem) -> int:
            haystack = _norm(f"{item.title} {item.tags or ''} {item.content}")
            title = _norm(f"{item.title} {item.tags or ''}")
            return sum(3 if term in title else 1 for term in terms if term in haystack)

        items.sort(key=score, reverse=True)
    text = "\n".join(f"- {item.title}: {item.content}" for item in items[:12])
    return text[:6000]


_SELLER_KB_STOPWORDS = {
    "que", "qual", "quais", "como", "quanto", "quantos", "quantas", "quando", "onde",
    "para", "pra", "com", "sem", "uma", "um", "por", "mais", "tem", "ter", "voce",
    "voces", "isso", "esse", "essa", "meu", "minha", "sulnet", "plano", "fica",
}

_SELLER_QUESTION_START = re.compile(
    r"^(e\s+)?(qual|quais|quanto|quantos|quantas|quando|como|onde|por que|porque|pq|"
    r"tem|terei|vou ter|posso|preciso|precisa|e se|o que|oque|existe|da pra|aceita|"
    r"inclui|funciona)\b"
)


class SellerKnowledgeAnswer(BaseModel):
    answered: bool = False
    answer: str = Field(default="", max_length=700)
    human_requested: bool = False


def _seller_is_question(text: str) -> bool:
    n = _norm(text)
    if len(n) < 6:
        return False
    starts_as_question = bool(_SELLER_QUESTION_START.match(n))
    if "?" not in (text or "") and not starts_as_question:
        return False
    # Respostas da etapa escritas como pergunta ("posso pagar no boleto?",
    # "sim, pode seguir?") continuam sendo respostas, não dúvidas.
    if _billing_local(text):
        return False
    return starts_as_question or _yes_no(text) == "NAO_INFORMADO"


def _seller_question_reply(
    db: Session,
    *,
    agent: AIAgentProfile,
    state: AIClosingState,
    collected: dict[str, Any],
    text: str,
) -> str | None:
    """Responde dúvidas do cliente no meio do fechamento usando a base aprovada.

    Antes, uma pergunta ("quanto tempo para instalar?") era lida como resposta da
    etapa e a IA repetia a pergunta anterior. Agora ela responde pela base de
    conhecimento do Vendedor e retoma a etapa em que parou.
    """
    if state.step not in SELLER_CUSTOMER_INPUT_STEPS or not _seller_is_question(text):
        return None
    if not settings.openai_api_key:
        return None
    knowledge = _seller_knowledge(db, agent, text)
    if not knowledge:
        return None
    from agents import Agent
    from app.services.ai_runtime import run_agent_sync

    plan = state.plan_label or "não informado"
    prompt = (
        "Você é o Vendedor IA da Sulnet no meio de um fechamento pelo WhatsApp. "
        "O cliente fez uma pergunta. Responda em português, em no máximo 3 frases curtas, "
        "usando SOMENTE os fatos da base abaixo. Não invente preço, prazo, desconto ou "
        "condição. Não faça nenhuma pergunta ao cliente: a próxima etapa será enviada "
        "depois da sua resposta. Se a base não tiver a resposta, devolva answered=false. "
        "Se o cliente pedir uma pessoa ou atendente, devolva human_requested=true.\n"
        f"Plano em negociação: {plan}.\n"
        f"Pergunta do cliente: {_redact(text)}\n"
        f"Base aprovada:\n{knowledge}"
    )
    answerer = Agent(
        name=f"{agent.name} · dúvidas",
        instructions=(agent.system_prompt or "").strip() or "Responda só com fatos aprovados.",
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 30.0,
        },
        output_type=SellerKnowledgeAnswer,
    )
    try:
        result = run_agent_sync(answerer, prompt)
        output = result.final_output
        answer = (
            output if isinstance(output, SellerKnowledgeAnswer)
            else SellerKnowledgeAnswer.model_validate(output)
        )
    except Exception:
        LOG.exception("seller_question_answer_failed step=%s", state.step)
        return None
    if answer.human_requested:
        return None
    body = answer.answer.strip()
    if not answer.answered or not body:
        # Vai para o analista semanal, que transforma em rascunho do manual.
        pending = list(collected.get("_unanswered_questions") or [])
        if text.strip() not in pending:
            pending.append(text.strip()[:300])
        collected["_unanswered_questions"] = pending[-10:]
        body = (
            "Essa informação eu não tenho confirmada aqui, e prefiro não te passar "
            "nada errado. Se quiser, posso chamar um consultor da Sulnet."
        )
    return f"{body}\n\n{_prompt_for_step(state, collected)}"


def _interpret_ambiguous(
    db: Session,
    *,
    agent: AIAgentProfile,
    step: str,
    message: str,
    plan_label: str,
) -> SellerInterpretation:
    if not settings.openai_api_key:
        return SellerInterpretation()
    from agents import Agent
    from app.services.ai_runtime import run_agent_sync

    instructions = (agent.system_prompt or "").strip()
    knowledge = _seller_knowledge(db, agent, message)
    prompt = (
        "Você está interpretando uma resposta curta no fechamento de uma contratação Sulnet. "
        "Não invente dados. Extraia somente o que o cliente realmente informou. "
        "Nunca trate silêncio/ambiguidade como confirmação. "
        f"Etapa atual: {step}. Plano em negociação: {plan_label or 'não informado'}.\n"
        f"Mensagem do cliente: {_redact(message)}\n"
        f"Conhecimento autorizado:\n{knowledge}"
    )
    interpreter = Agent(
        name=f"{agent.name} · interpretação",
        instructions=instructions or "Interprete respostas de fechamento com conservadorismo.",
        model=agent.model or "gpt-6-luna",
        model_settings={
            "reasoning": {"effort": agent.reasoning_effort or "low"},
            "verbosity": "low",
            "timeout": 30.0,
        },
        output_type=SellerInterpretation,
    )
    try:
        result = run_agent_sync(interpreter, prompt)
        output = result.final_output
        return output if isinstance(output, SellerInterpretation) else SellerInterpretation.model_validate(output)
    except Exception:
        LOG.exception("seller_interpretation_failed step=%s", step)
        return SellerInterpretation()


def _assert_closer_owns_conversation(
    db: Session,
    *,
    conversation: Conversation,
    agent: AIAgentProfile,
    handoff_notice: bool = False,
) -> None:
    ai_state = db.scalar(
        select(AIConversationState).where(
            AIConversationState.conversation_id == conversation.id
        )
    )
    # O aviso de transferência é a última mensagem do Vendedor: nesse momento a
    # conversa já foi passada para um humano (HANDOFF_HUMAN) por este mesmo agente.
    allowed_states = {"CLOSING", "HANDOFF_HUMAN"} if handoff_notice else {"CLOSING"}
    if (
        not ai_state
        or ai_state.state not in allowed_states
        or int(ai_state.agent_id or 0) != int(agent.id)
        or agent.role_type != "CLOSER"
    ):
        raise CRMIntegrationError(
            "Vendedor IA tentou enviar sem propriedade exclusiva da conversa."
        )


def _is_fixed_closing_text(closing: AIClosingState | None, text: str) -> bool:
    """Textos montados pelo código a partir do estado, sem geração do modelo.

    O resumo para aceite e o link de assinatura não passam pelo Guardião: em
    04/11 (conversa 472) o Guardião barrou o resumo a cada minuto por "campo
    pendente no playbook" (forma de pagamento, que o CRM preenche no
    contract/generate) e o cliente nunca recebeu o contrato.
    """
    if not closing or not text.strip():
        return False
    if text.startswith(black_campaign.FIXED_PREFIXES) or text.startswith(_CLOSER_NOTICE_PREFIXES):
        return True
    if closing.step == SALE_ACCEPTANCE_STEP and text.startswith(
        ("Antes de gerar o contrato, confira o resumo", "Só preciso da sua confirmação para gerar o contrato")
    ):
        return True
    if (
        text == SIGNED_FINAL_MESSAGE
        and closing.step == "COMPLETED"
        and _contract_is_signed(str(closing.contract_status or ""))
    ):
        return True
    sign_url = str(closing.sign_url or "").strip()
    return bool(sign_url) and sign_url in text and closing.step == "WAITING_SIGNATURE"


def _is_step_prompt(closing: AIClosingState | None, text: str) -> bool:
    """Pergunta da etapa montada pelo código ou publicada pelo playbook do CRM.

    Em 04/11 (conversa 518) o Guardião barrou "qual o número a portar?" por
    PLAYBOOK_SEQUENCE_MISMATCH e o cliente ficou parado. Para essas perguntas o
    Guardião pode reescrever, mas nunca impedir o envio.
    """
    if not closing or not text.strip():
        return False
    collected = _load_collected(closing)
    expected = {
        str(_prompt_for_step(closing, collected) or "").strip(),
        str(collected.get("_playbook_question") or "").strip(),
    }
    expected.discard("")
    return text.strip() in expected


def _send_text(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    text: str,
    source: str = "CLOSER_TEXT",
    guardian_metadata: dict[str, Any] | None = None,
    handoff_notice: bool = False,
) -> Message:
    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    if not text.strip():
        raise ValueError("Mensagem vazia.")
    _assert_closer_owns_conversation(
        db,
        conversation=conversation,
        agent=agent,
        handoff_notice=handoff_notice,
    )
    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone:
        raise CRMIntegrationError("Contato sem telefone para WhatsApp.")
    closing = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == conversation.id
        )
    )
    fixed_buttons: list[dict[str, str]] = []
    if _is_fixed_closing_text(closing, text):
        if closing.step == SALE_ACCEPTANCE_STEP:
            fixed_buttons = _seller_step_buttons(closing.step, text, _load_collected(closing))
    else:
        guardian = guard_outbound_message(
            db,
            agent=agent,
            conversation=conversation,
            reply_text=text,
            buttons=[],
            source=source,
            proposed_state=str(closing.step if closing else ""),
            metadata=(
                guardian_metadata
                or {"closing_step": str(closing.step if closing else "")}
            ),
        )
        if not guardian.should_send and not _is_step_prompt(closing, text):
            raise GuardianBlockedError(guardian)
        if not guardian.should_send:
            LOG.warning(
                "seller_guardian_override conversation=%s issues=%s",
                conversation.id,
                ",".join(guardian.issue_codes)[:200] or "-",
            )
            fixed_buttons = []
        else:
            if guardian.verdict in {"REWRITE", "CORRECT_STATE"}:
                text = guardian.reply_text
            fixed_buttons = list(guardian.buttons)

    zapi = service_for_conversation(db, conversation.id)
    if len(fixed_buttons) >= 2:
        result = zapi.send_button_list(
            phone=phone,
            message=text,
            buttons=fixed_buttons,
        )
        message_type = "BUTTONS"
    else:
        result = zapi.send_text(phone=phone, message=text)
        message_type = "TEXT"
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=message_type,
        body=text,
        sender_user_id=agent.user_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    db.flush()
    return message


def _send_fixed_step_prompt(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    text: str,
) -> Message:
    """Envia o texto fixo de uma etapa do fechamento sem a revisão do Guardião."""
    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone or not text.strip():
        raise CRMIntegrationError("Contato sem telefone ou mensagem vazia.")
    result = service_for_conversation(db, conversation.id).send_text(phone=phone, message=text)
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=text,
        sender_user_id=agent.user_id,
        provider_message_id=str(result.get("messageId") or result.get("id") or "").strip() or None,
        provider_zaap_id=str(result.get("zaapId") or "").strip() or None,
        status="SENT",
    )
    db.flush()
    return message


def _seller_step_buttons(
    step: str,
    text: str = "",
    collected: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    current_collected = collected or {}
    playbook_step = str(current_collected.get("_playbook_step") or "")
    if playbook_step == step:
        playbook_options = _playbook_current_options(current_collected)
        buttons = [
            {
                "id": str(item.get("id") or f"option_{index}")[:40],
                "label": str(item.get("label") or "")[:20],
            }
            for index, item in enumerate(playbook_options[:3], start=1)
            if str(item.get("label") or "").strip()
        ]
        if len(buttons) >= 2:
            return buttons

    if step == "PLAYBOOK_DYNAMIC_FIELD":
        dynamic = _playbook_dynamic_options(collected or {})
        buttons = [
            {
                "id": str(item.get("id") or f"option_{index}")[:40],
                "label": str(item.get("label") or "")[:20],
            }
            for index, item in enumerate(dynamic[:3], start=1)
            if str(item.get("label") or "").strip()
        ]
        if len(buttons) >= 2:
            return buttons

    if step == "COMBO_MOBILE_PLAN":
        return black_campaign.combo_page_buttons(int(current_collected.get("_combo_page") or 1))
    if step == "MOBILE_CONTRACT_TYPE" and _sdr_ia_mode(collected or {}):
        return [dict(item) for item in MOBILE_CONTRACT_BUTTONS_SDR_IA]
    if step == "BILLING":
        # Dias de vencimento aceitos dependem do CRM; por isso só a forma de
        # pagamento ganha botões.
        if not current_collected.get("paymentMethod"):
            return [dict(item) for item in SELLER_STEP_BUTTONS["BILLING_PAYMENT_METHOD"]]
        return []
    buttons = SELLER_STEP_BUTTONS.get(step)
    if buttons:
        return [dict(item) for item in buttons]

    # Etapas que pedem dado digitável: nunca inferir Sim/Não.
    # Em 04/11 o playbook "Qual número você deseja portar?" gerou botões
    # Sim/Não por causa do cue "deseja" em infer_quick_replies.
    free_text_steps = {
        "NAME",
        "CPF",
        "BIRTH_DATE",
        "CEP",
        "ADDRESS_DETAILS",
        "ADDRESS_NUMBER",
        "EMAIL",
        "MOBILE_NEW_ICCID",
        "MOBILE_NEW_NUMBER",
        "MOBILE_PORT_ICCID",
        "MOBILE_PORT_NUMBER",
        "MOBILE_PORT_NAME",
        "MOBILE_PORT_DOC",
        "MOBILE_PORT_DATE",
        "MOBILE_M2M_QTY",
        "MOBILE_M2M_ICCIDS",
        "MOBILE_DUE_DAY",
        "MOBILE_ACTIVATION_VALUE",
        "MOBILE_DEBIT_BANK",
        "MOBILE_DEBIT_AGENCY",
        "MOBILE_DEBIT_ACCOUNT",
        "MOBILE_DEBIT_HOLDER",
        "MOBILE_UNIFY_REASON",
        "BILLING_DUE_DAY",
        "BILLING_ACTIVATION_VALUE",
        "BILLING_DEBIT_BANK",
        "BILLING_DEBIT_AGENCY",
        "BILLING_DEBIT_ACCOUNT",
        "BILLING_DEBIT_HOLDER",
        "BILLING_UNIFY_REASON",
    }
    if step in free_text_steps:
        return []

    # Segurança adicional: perguntas previsíveis (ex.: sim/não) viram botões,
    # mas perguntas de dado livre ficam sem botão.
    from app.services.ai_quick_replies import quick_replies_for_message

    return quick_replies_for_message(text, None)


def _send_step_message(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    state: AIClosingState,
    text: str,
    authoritative_contract_knowledge: bool = False,
) -> Message:
    _assert_closer_owns_conversation(
        db,
        conversation=conversation,
        agent=agent,
        handoff_notice=state.step == "ESCALATED",
    )
    recent_duplicate = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction == "OUTBOUND",
            Message.sender_user_id == agent.user_id,
            Message.body == text,
            Message.created_at >= utcnow() - timedelta(minutes=5),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    latest_message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    if (
        recent_duplicate
        and latest_message
        and latest_message.direction == "OUTBOUND"
    ):
        LOG.info(
            "seller_duplicate_suppressed conversation=%s message=%s",
            conversation.id,
            recent_duplicate.id,
        )
        return recent_duplicate

    collected = _load_collected(state)
    guardian_metadata = _playbook_recovery_metadata(
        state,
        collected,
    )
    if authoritative_contract_knowledge:
        guardian_metadata = {
            **guardian_metadata,
            "authoritative_contract_knowledge": True,
            "knowledge_source": "FTTH_CONTRACT_POLICY",
        }
    buttons = _seller_step_buttons(
        state.step,
        text,
        collected,
    )
    if len(buttons) < 2:
        return _send_text(
            db,
            agent=agent,
            conversation=conversation,
            text=text,
            source="CLOSER_STEP",
            guardian_metadata=guardian_metadata,
            handoff_notice=state.step == "ESCALATED",
        )

    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone:
        raise CRMIntegrationError("Contato sem telefone para WhatsApp.")
    if not _is_fixed_closing_text(state, text):
        guardian = guard_outbound_message(
            db,
            agent=agent,
            conversation=conversation,
            reply_text=text,
            buttons=buttons,
            source="CLOSER_STEP",
            proposed_state=state.step,
            metadata=guardian_metadata,
        )
        if not guardian.should_send and not _is_step_prompt(state, text):
            raise GuardianBlockedError(guardian)
        if not guardian.should_send:
            LOG.warning(
                "seller_guardian_override conversation=%s issues=%s",
                conversation.id,
                ",".join(guardian.issue_codes)[:200] or "-",
            )
        elif guardian.verdict in {"REWRITE", "CORRECT_STATE"}:
            text = guardian.reply_text
            buttons = list(guardian.buttons or [])

    # Guardião pode reescrever e zerar os botões. Sem ≥2 labels a Z-API
    # estoura "Informe ao menos um botao" (conv 485 em produção).
    if len(buttons) < 2:
        return _send_text(
            db,
            agent=agent,
            conversation=conversation,
            text=text,
            source="CLOSER_STEP",
            guardian_metadata=guardian_metadata,
            handoff_notice=state.step == "ESCALATED",
        )

    zapi = service_for_conversation(db, conversation.id)
    result = zapi.send_button_list(
        phone=phone,
        message=text,
        buttons=buttons,
    )
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="BUTTONS",
        body=text,
        sender_user_id=agent.user_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    db.flush()
    return message


def _record_activity(
    db: Session,
    *,
    conversation: Conversation,
    agent: AIAgentProfile,
    outcome: str,
    description: str,
) -> None:
    if not conversation.lead_id:
        return
    db.add(
        Activity(
            lead_id=conversation.lead_id,
            user_id=agent.user_id,
            kind="IA_VENDEDOR",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=outcome[:120],
            description=description[:5000],
            completed_at=utcnow(),
        )
    )


def _create_crm_if_ready(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
) -> None:
    if state.opportunity_id:
        return
    collected = _load_collected(state)
    name = str(collected.get("name") or "").strip()
    phone = _digits(str(collected.get("phone") or conversation.contact.phone or ""))
    if not _looks_like_full_name(name) or len(phone) < 10:
        return

    client = SulnetCRMClient.from_settings()
    payload: dict[str, Any] = {
        "externalId": state.external_id,
        "conversationId": conversation.id,
        "leadId": conversation.lead_id,
        "clientName": name,
        "clientPhone": phone,
        "planLabel": state.plan_label or None,
        "monthlyValue": _money_for_api(state.offer_price),
        "sdrSummary": state.handoff_summary,
        "handoffReason": collected.get("handoffReason") or "",
        "runConsult": False,
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    if black_campaign.has_marker(state.handoff_summary):
        extras = {
            "tags": [black_campaign.CAMPAIGN_TAG],
            "campaign": black_campaign.CAMPAIGN_TAG,
            "firstDueDate": black_campaign.FIRST_DUE_ISO,
        }
        try:
            result = client.create_opportunity({**payload, **extras})
        except CRMIntegrationError as exc:
            # Campos extras que o CRM não conhece nunca podem travar a venda.
            if exc.status_code not in (400, 422):
                raise
            LOG.warning("crm_black_extras_rejected status=%s", exc.status_code)
            result = client.create_opportunity(payload)
    else:
        result = client.create_opportunity(payload)
    state.opportunity_id = int(result.get("opportunityId") or 0) or None
    state.protocol = str(result.get("protocol") or "")
    if not state.opportunity_id:
        raise CRMIntegrationError("CRM não retornou o identificador da oportunidade.")
    if black_campaign.has_marker(state.handoff_summary):
        _apply_black_tag(client, state)


def _apply_black_tag(client: SulnetCRMClient, state: AIClosingState) -> None:
    """Tag "Promoção Black 2027" na oportunidade; falha da tag nunca trava a venda."""
    if not state.opportunity_id:
        return
    try:
        client.add_opportunity_tag(state.opportunity_id, black_campaign.CAMPAIGN_TAG)
    except CRMIntegrationError as exc:
        LOG.warning("crm_tag_failed opportunity=%s status=%s", state.opportunity_id, exc.status_code)


COMBO_MOBILE_STEP = "COMBO_MOBILE_PLAN"
_COMBO_KEEP_KEYS = {"_black_combo", "_sdr_ia_funnel", "_sdr_ia_defaults", "handoffReason", "freeMonths", "concessionLevel"}
_COMBO_DROP_KEYS = {
    "planId", "categoryId", "contractProductName", "dynamicPlaybookValues",
    "crmPlaybook", "categoryFormValues", "mobileCategoryValues", "categoryName",
    "catalogPlanName", "catalogMonthlyValue", "catalogPlanId", "creditStatus",
    "paymentMethod", "dueDay", "invoiceDelivery",
}


def _black_combo_pending(collected: dict[str, Any]) -> bool:
    combo = collected.get("_black_combo")
    return isinstance(combo, dict) and combo.get("stage") == black_campaign.COMBO_STAGE_FIBER


def _combo_after_first_signature(state: AIClosingState, collected: dict[str, Any]) -> str | None:
    """Combo Black: a fibra foi assinada, abre a 2ª venda (móvel) na mesma conversa."""
    if not _black_combo_pending(collected):
        return None
    combo = collected["_black_combo"]
    combo["stage"] = black_campaign.COMBO_STAGE_MOBILE
    combo["fiber"] = {
        "opportunity_id": state.opportunity_id,
        "protocol": state.protocol,
        "plan": state.plan_label,
    }
    for key in list(collected):
        if key in _COMBO_KEEP_KEYS:
            continue
        if key in _COMBO_DROP_KEYS or key.startswith("_") or "playbook" in key.lower() or key.startswith("catalog"):
            collected.pop(key, None)
    state.external_id = f"{state.external_id}-movel"[:120]
    state.opportunity_id = None
    state.protocol = ""
    state.sign_url = ""
    state.contract_status = ""
    state.credit_status = ""
    state.credit_analysis_id = None
    state.completed_at = None
    state.plan_label = ""
    state.offer_price = None
    state.step = COMBO_MOBILE_STEP
    return black_campaign.combo_mobile_prompt()


def _combo_choose_mobile(state: AIClosingState, collected: dict[str, Any], text: str) -> str:
    choice = black_campaign.mobile_choice_from_text(text)
    if choice:
        state.plan_label = choice["name"]
        state.offer_price = _money(choice["price"])
        state.step = "CPF"
        return black_campaign.combo_cpf_prompt(choice["name"], choice["price"])
    page = int(collected.get("_combo_page") or 1)
    folded = _norm(text)
    if re.search(r"\b(mais planos|ver mais|mais opcoes|outros planos)\b", folded):
        page = page + 1 if page < black_campaign.combo_page_count() else 1
        collected["_combo_page"] = page
        return black_campaign.combo_mobile_prompt(page)
    if folded in {"voltar", "inicio"}:
        collected["_combo_page"] = 1
        return black_campaign.combo_mobile_prompt(1)
    if _yes_no(text) == "NAO" or re.search(r"\b(desist\w*|cancel\w*|nao quero|sem movel)\b", folded):
        collected["_black_combo"]["stage"] = black_campaign.COMBO_STAGE_DECLINED
        state.step = "COMPLETED"
        state.completed_at = utcnow()
        return black_campaign.COMBO_DECLINED_MESSAGE
    return black_campaign.combo_mobile_prompt(page)


def _after_signed_text(state: AIClosingState, collected: dict[str, Any]) -> str:
    """Mensagem depois de uma assinatura: próxima venda do combo ou o encerramento."""
    if state.step == "COMPLETED":
        combo = _combo_after_first_signature(state, collected)
        if combo:
            return combo
    return SIGNED_FINAL_MESSAGE


def _prompt_for_step(state: AIClosingState, collected: dict[str, Any]) -> str:
    if state.step == COMBO_MOBILE_STEP:
        return black_campaign.combo_mobile_prompt(int(collected.get("_combo_page") or 1))
    if _sdr_ia_mode(collected) and state.step in SDR_IA_FORBIDDEN_STEPS:
        return (
            "Vou seguir apenas com os dados necessários para concluir sua contratação "
            "pelo canal digital."
        )

    playbook_question = str(collected.get("_playbook_question") or "").strip()
    playbook_step = str(collected.get("_playbook_step") or "").strip()
    if playbook_question and playbook_step == state.step:
        return playbook_question

    plan = state.plan_label or "plano combinado"
    if state.step == "NAME":
        return f"Perfeito! Vamos dar início à contratação do {plan}. Para começar, me confirma seu nome completo?"
    if state.step == "CPF":
        return "Ótimo. Agora preciso do seu CPF para realizar a consulta cadastral e seguir com a contratação."
    if state.step == "BIRTH_DATE":
        return "Para concluir a consulta cadastral, preciso também da sua data de nascimento. Pode me enviar no formato DD/MM/AAAA?"
    if state.step == "CEP":
        if _is_mobile_plan(state, collected):
            return "Consulta aprovada! Qual é o CEP do seu endereço de cadastro?"
        return "Consulta aprovada! Qual é o CEP do endereço onde será instalada a internet?"
    if state.step == "ADDRESS_DETAILS":
        return "Qual é a rua ou o bairro que ficou faltando no endereço?"
    if state.step == "ADDRESS_NUMBER":
        street = str(collected.get("street") or "").strip()
        city = str(collected.get("city") or "").strip()
        prefix = f"Localizei {street}" if street else "Localizei o endereço"
        if city:
            prefix += f", em {city}"
        return prefix + ". Qual é o número e, se houver, o complemento?"
    if state.step == "PLAN_CONFIRM":
        return f"Perfeito. O plano negociado ficou {plan}. Confirma que seguimos com ele?"
    if state.step == "EMAIL":
        return "Certo. Qual e-mail você quer deixar no cadastro e receber as comunicações da contratação?"
    if state.step == "MOBILE_CONTRACT_TYPE":
        return "Como você quer contratar o Sulmóvel?"
    if state.step == "MOBILE_NEW_ICCID":
        return "Qual é o ICCID do chip da nova linha?"
    if state.step == "MOBILE_NEW_NUMBER":
        return "Qual é o número com DDD da nova linha?"
    if state.step == "MOBILE_PORT_ICCID":
        return "Qual é o ICCID do chip da linha que será portada?"
    if state.step == "MOBILE_PORT_NUMBER":
        return "Qual é o número que será portado?"
    if state.step == "MOBILE_PORT_TITULAR":
        return "A portabilidade está em nome do titular desta contratação? Responda sim ou não."
    if state.step == "MOBILE_PORT_NAME":
        return "Qual é o nome completo do titular da linha que será portada?"
    if state.step == "MOBILE_PORT_DOC":
        return "Qual é o CPF ou CNPJ do titular da linha que será portada?"
    if state.step == "MOBILE_PORT_DATE":
        return "Qual é a data prevista para a portabilidade? Pode enviar no formato DD/MM/AAAA."
    if state.step == "MOBILE_M2M_QTY":
        return "Quantos chips M2M serão contratados?"
    if state.step == "MOBILE_M2M_ICCIDS":
        return "Envie os ICCIDs dos chips M2M, separados por espaço ou vírgula."
    if state.step == "MOBILE_DUE_DAY":
        return "Qual dia de vencimento você prefere?"
    if state.step == "MOBILE_ACTIVATION_FEE":
        return "Existe taxa de ativação nesta contratação?"
    if state.step == "MOBILE_ACTIVATION_VALUE":
        return "Qual é o valor da taxa de ativação?"
    if state.step == "MOBILE_PAYMENT_METHOD":
        return "Qual forma de pagamento você prefere: boleto ou débito em conta?"
    if state.step == "MOBILE_INVOICE_DELIVERY":
        return "Como quer receber a fatura: WhatsApp, e-mail ou ambos?"
    if state.step == "MOBILE_DEBIT_BANK":
        return "Para o débito em conta, qual é o banco?"
    if state.step == "MOBILE_DEBIT_AGENCY":
        return "Qual é a agência?"
    if state.step == "MOBILE_DEBIT_ACCOUNT":
        return "Qual é a conta?"
    if state.step == "MOBILE_DEBIT_HOLDER":
        return "Qual é o nome do titular da conta bancária?"
    if state.step == "MOBILE_UNIFY":
        return "Deseja unificar esta fatura com outras faturas Sulnet? Responda sim ou não."
    if state.step == "MOBILE_UNIFY_REASON":
        return "Qual é o motivo para não unificar as faturas?"
    if state.step == "BILLING_PAYMENT_METHOD":
        return "Qual forma de pagamento você prefere: boleto ou débito em conta?"
    if state.step == "BILLING_INVOICE_DELIVERY":
        return "Como quer receber a fatura: WhatsApp, e-mail ou ambos?"
    if state.step == "BILLING_DUE_DAY":
        return "Qual dia de vencimento você prefere?"
    if state.step == "BILLING_ACTIVATION_FEE":
        return "Existe taxa de ativação nesta contratação?"
    if state.step == "BILLING_ACTIVATION_VALUE":
        return "Qual é o valor da taxa de ativação?"
    if state.step == "BILLING_DEBIT_BANK":
        return "Para o débito em conta, qual é o banco?"
    if state.step == "BILLING_DEBIT_AGENCY":
        return "Qual é a agência?"
    if state.step == "BILLING_DEBIT_ACCOUNT":
        return "Qual é a conta?"
    if state.step == "BILLING_DEBIT_HOLDER":
        return "Qual é o nome do titular da conta bancária?"
    if state.step == "BILLING_UNIFY":
        return "Deseja unificar esta fatura com outras faturas Sulnet?"
    if state.step == "BILLING_UNIFY_REASON":
        return "Qual é o motivo para não unificar as faturas?"
    if state.step == "BILLING":
        if not collected.get("paymentMethod"):
            return "Qual forma de pagamento você prefere: boleto ou débito em conta?"
        if not collected.get("dueDay"):
            return "Certo. Qual dia de vencimento você prefere?"
        return "Vou confirmar os dados de faturamento para seguir."
    if state.step == SALE_ACCEPTANCE_STEP:
        return _sale_acceptance_prompt(state, collected, reminder=True)
    if state.step == "CREDIT_REJECTED":
        return "A consulta cadastral não foi aprovada. Quer tentar a contratação com outro CPF?"
    if state.step == "WAITING_CRM_READY":
        return (
            "Seus dados já foram enviados ao CRM. Estou aguardando a validação final "
            "para preparar o contrato e sigo automaticamente por aqui assim que liberar."
        )
    if state.step == "WAITING_CONTRACT":
        return "Seu cadastro comercial já foi concluído e o contrato está sendo preparado. Assim que o link de assinatura estiver disponível, eu envio por aqui."
    if state.step == "WAITING_SIGNATURE":
        return "Seu contrato já foi enviado. Assim que você concluir a assinatura, eu confirmo a contratação por aqui."
    return "Vou seguir com a próxima etapa da sua contratação."


SELLER_CUSTOMER_INPUT_STEPS = {
    "COMBO_MOBILE_PLAN",
    "CONFIRM_SALE",
    "NAME",
    "CPF",
    "BIRTH_DATE",
    "CEP",
    "ADDRESS_DETAILS",
    "ADDRESS_NUMBER",
    "PLAN_CONFIRM",
    "EMAIL",
    "PLAYBOOK_DYNAMIC_FIELD",
    "BILLING",
    "CREDIT_REJECTED",
    "MOBILE_CONTRACT_TYPE",
    "MOBILE_NEW_ICCID",
    "MOBILE_NEW_NUMBER",
    "MOBILE_PORT_ICCID",
    "MOBILE_PORT_NUMBER",
    "MOBILE_PORT_TITULAR",
    "MOBILE_PORT_NAME",
    "MOBILE_PORT_DOC",
    "MOBILE_PORT_DATE",
    "MOBILE_M2M_QTY",
    "MOBILE_M2M_ICCIDS",
    "MOBILE_DUE_DAY",
    "MOBILE_ACTIVATION_FEE",
    "MOBILE_ACTIVATION_VALUE",
    "MOBILE_PAYMENT_METHOD",
    "MOBILE_INVOICE_DELIVERY",
    "MOBILE_DEBIT_BANK",
    "MOBILE_DEBIT_AGENCY",
    "MOBILE_DEBIT_ACCOUNT",
    "MOBILE_DEBIT_HOLDER",
    "MOBILE_UNIFY",
    "MOBILE_UNIFY_REASON",
    "BILLING_PAYMENT_METHOD",
    "BILLING_INVOICE_DELIVERY",
    "BILLING_DUE_DAY",
    "BILLING_ACTIVATION_FEE",
    "BILLING_ACTIVATION_VALUE",
    "BILLING_DEBIT_BANK",
    "BILLING_DEBIT_AGENCY",
    "BILLING_DEBIT_ACCOUNT",
    "BILLING_DEBIT_HOLDER",
    "BILLING_UNIFY",
    "BILLING_UNIFY_REASON",
    "WAITING_SIGNATURE",
}


def _seller_followup_prompt(
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    followup_step: int,
) -> str:
    if _sdr_ia_mode(collected) and state.step in SDR_IA_FORBIDDEN_STEPS:
        return (
            "Para continuarmos de onde paramos, vou seguir apenas com os dados "
            "necessários para a contratação digital."
        )

    plan = state.plan_label or "plano combinado"
    prefix = (
        "Para continuarmos de onde paramos, "
        if followup_step <= 1
        else "Quando puder, seguimos exatamente de onde paramos: "
    )
    playbook_question = str(collected.get("_playbook_question") or "").strip()
    playbook_step = str(collected.get("_playbook_step") or "").strip()
    if playbook_question and playbook_step == state.step:
        return playbook_question

    prompts = {
        "NAME": "me envie seu nome completo para eu iniciar o cadastro.",
        "CPF": "me envie seu CPF para eu continuar a consulta cadastral.",
        "BIRTH_DATE": "me envie sua data de nascimento no formato DD/MM/AAAA.",
        "CEP": "me envie o CEP do endereço para eu continuar a contratação.",
        "ADDRESS_DETAILS": "me informe a rua ou o bairro que ficou faltando.",
        "ADDRESS_NUMBER": "me informe o número do imóvel e, se houver, o complemento.",
        "EMAIL": "me informe o e-mail que ficará no cadastro.",
        "MOBILE_NEW_ICCID": "me informe o ICCID do chip da nova linha.",
        "MOBILE_NEW_NUMBER": "me informe o número com DDD da nova linha.",
        "MOBILE_PORT_ICCID": "me informe o ICCID do chip da linha que será portada.",
        "MOBILE_PORT_NUMBER": "me informe o número com DDD que será portado.",
        "MOBILE_PORT_NAME": "me informe o nome completo do titular da linha.",
        "MOBILE_PORT_DOC": "me informe o CPF ou CNPJ do titular da linha.",
        "MOBILE_PORT_DATE": "me informe a data prevista da portabilidade.",
        "MOBILE_M2M_QTY": "me informe a quantidade de chips M2M.",
        "MOBILE_M2M_ICCIDS": "me envie todos os ICCIDs dos chips M2M.",
        "MOBILE_DUE_DAY": "me informe o dia de vencimento.",
        "MOBILE_ACTIVATION_VALUE": "me informe o valor da taxa de ativação.",
        "MOBILE_DEBIT_BANK": "me informe o banco para o débito em conta.",
        "MOBILE_DEBIT_AGENCY": "me informe a agência.",
        "MOBILE_DEBIT_ACCOUNT": "me informe a conta.",
        "MOBILE_DEBIT_HOLDER": "me informe o nome do titular da conta bancária.",
        "MOBILE_UNIFY_REASON": "me informe o motivo para manter as faturas separadas.",
        "BILLING_DUE_DAY": "me informe o dia de vencimento.",
        "BILLING_ACTIVATION_VALUE": "me informe o valor da taxa de ativação.",
        "BILLING_DEBIT_BANK": "me informe o banco para o débito em conta.",
        "BILLING_DEBIT_AGENCY": "me informe a agência.",
        "BILLING_DEBIT_ACCOUNT": "me informe a conta.",
        "BILLING_DEBIT_HOLDER": "me informe o nome do titular da conta bancária.",
        "BILLING_UNIFY_REASON": "me informe o motivo para manter as faturas separadas.",
        "BILLING": "me informe a forma de pagamento/faturamento e o dia de vencimento.",
    }
    if state.step == "PLAN_CONFIRM":
        return f"Para seguirmos com a contratação do {plan}, confirma esse plano?"
    if state.step == "MOBILE_CONTRACT_TYPE":
        return "Para continuar com o Sulmóvel, escolha como deseja a linha:"
    if state.step == "MOBILE_PORT_TITULAR":
        return "Para continuar a portabilidade, a linha está no nome do titular desta contratação?"
    if state.step == "MOBILE_ACTIVATION_FEE":
        return "Para continuar, informe se existe taxa de ativação:"
    if state.step == "MOBILE_PAYMENT_METHOD":
        return "Para continuar, escolha a forma de pagamento:"
    if state.step == "MOBILE_INVOICE_DELIVERY":
        return "Para continuar, escolha como quer receber a fatura:"
    if state.step == "MOBILE_UNIFY":
        return "Para continuar, deseja unificar esta fatura com outras faturas Sulnet?"
    if state.step == "BILLING_PAYMENT_METHOD":
        return "Para continuar, escolha a forma de pagamento:"
    if state.step == "BILLING_INVOICE_DELIVERY":
        return "Para continuar, escolha como quer receber a fatura:"
    if state.step == "BILLING_ACTIVATION_FEE":
        return "Para continuar, informe se existe taxa de ativação:"
    if state.step == "BILLING_UNIFY":
        return "Para continuar, deseja unificar esta fatura com outras faturas Sulnet?"
    if state.step == "CREDIT_REJECTED":
        return "Se quiser continuar com outro titular, escolha Tentar outro CPF. Se não quiser continuar, escolha Encerrar."
    if state.step == "WAITING_SIGNATURE":
        if state.sign_url:
            return f"Seu contrato continua aguardando assinatura. Quando puder, assine por aqui: {state.sign_url}"
        return "Seu contrato continua aguardando assinatura. Assim que o link estiver disponível, sigo por aqui."
    return prefix + prompts.get(state.step, _prompt_for_step(state, collected))


def _schedule_seller_followup(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
    source_message_id: int | None,
    step: int = 1,
) -> None:
    if state.step not in SELLER_CUSTOMER_INPUT_STEPS:
        return
    queue = queue_for_conversation(db, conversation.id)
    if not queue:
        return
    from app.services.ai_followups import schedule_followup

    schedule_followup(
        db,
        agent_id=state.agent_id,
        queue_id=queue.id,
        conversation_id=conversation.id,
        source_message_id=source_message_id,
        step=step,
    )


def process_seller_followup(
    db: Session,
    job: AIFollowupJob,
    agent: AIAgentProfile,
) -> str:
    conversation = db.get(Conversation, job.conversation_id)
    state = db.scalar(
        select(AIClosingState).where(
            AIClosingState.conversation_id == job.conversation_id
        )
    )
    if (
        not conversation
        or not state
        or state.step not in SELLER_CUSTOMER_INPUT_STEPS
        or state.step in {"ESCALATED", "COMPLETED"}
    ):
        job.status = "CANCELADO"
        job.error = "FECHAMENTO_NAO_AGUARDA_CLIENTE"
        return "cancelled"

    collected = _load_collected(state)
    text = _seller_followup_prompt(
        state,
        collected,
        followup_step=max(1, int(job.step or 1)),
    )
    try:
        message = _send_step_message(
            db,
            agent=agent,
            conversation=conversation,
            state=state,
            text=text,
        )
    except GuardianBlockedError as exc:
        # Antes o erro escapava, o worker desfazia a transação e o mesmo
        # follow-up voltava a cada 10 minutos, para sempre (conversa 240).
        # Um follow-up é opcional: se o Guardião barrar, não insiste.
        codes = ", ".join(exc.result.issue_codes) or "-"
        job.status = "CANCELADO"
        job.error = f"GUARDIAO_BLOQUEOU_FOLLOWUP: {codes}"[:1200]
        write_audit(
            db,
            user=agent.user,
            action="AI_CLOSER_FOLLOWUP_BLOCKED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "followup_job_id": job.id,
                "step": job.step,
                "closing_step": state.step,
                "issue_codes": list(exc.result.issue_codes),
            },
        )
        return "guardian_blocked"
    job.status = "ENVIADO"
    job.sent_at = utcnow()
    job.error = None
    write_audit(
        db,
        user=agent.user,
        action="AI_CLOSER_FOLLOWUP_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "followup_job_id": job.id,
            "step": job.step,
            "closing_step": state.step,
            "message_id": message.id,
            "buttons": len(_seller_step_buttons(state.step, text, collected)),
        },
    )
    _schedule_seller_followup(
        db,
        state=state,
        conversation=conversation,
        source_message_id=job.source_message_id,
        step=max(1, int(job.step or 1)) + 1,
    )
    return "sent"


def resume_simplified_mobile_states(db: Session, *, limit: int = 50, since=None) -> int:
    """Reposiciona fechamentos antigos após mudanças de política do Vendedor IA.

    Para oportunidades confirmadas como funil SDR IA, remove estados que agora
    são proibidos (ICCID, M2M, vencimento, taxa e unificação) e volta a consultar
    o playbook. Outros funis mantêm o formulário completo.
    """
    candidate_steps = sorted({"BILLING", *SDR_IA_FORBIDDEN_STEPS})
    states = list(
        db.scalars(
            select(AIClosingState)
            .where(
                AIClosingState.step.in_(candidate_steps),
                AIClosingState.updated_at >= max(utcnow() - timedelta(days=2), since) if since else AIClosingState.updated_at >= utcnow() - timedelta(days=2),
            )
            .order_by(AIClosingState.updated_at.desc())
            .limit(limit)
        )
    )
    resumed = 0
    client: SulnetCRMClient | None = None

    for state in states:
        collected = _load_collected(state)
        sdr_ia_funnel = _sdr_ia_mode(collected)

        if sdr_ia_funnel:
            values = _category_values(collected)
            if _is_mobile_plan(state, collected):
                fallback_step = _mobile_next_step(
                    values,
                    sdr_ia_funnel=True,
                ) or "WAITING_CRM_READY"
            else:
                fallback_step = _billing_next_step(
                    values,
                    sdr_ia_funnel=True,
                ) or "WAITING_CRM_READY"

            next_step = fallback_step
            question: str | None = None
            if state.opportunity_id:
                try:
                    if client is None:
                        client = SulnetCRMClient.from_settings()
                    next_step, question = _query_playbook_guidance(
                        client,
                        state,
                        collected,
                        fallback_step=fallback_step,
                    )
                except CRMIntegrationError as exc:
                    LOG.warning(
                        "seller_sdr_ia_policy_resume_playbook_failed state=%s error=%s",
                        state.id,
                        str(exc)[:300],
                    )

            if next_step == "__FINISH__":
                next_step = "WAITING_CRM_READY"
                question = None

            state.step = next_step
            state.last_error = ""
            _save_collected(state, collected)

            conversation = db.get(Conversation, state.conversation_id)
            agent = db.get(AIAgentProfile, state.agent_id)
            if (
                state.step in SELLER_CUSTOMER_INPUT_STEPS
                and conversation
                and agent
                and agent.active
                and agent.execution_mode == "AUTO"
                and conversation.status not in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
            ):
                prompt = question or _prompt_for_step(state, collected)
                _send_step_message(
                    db,
                    agent=agent,
                    conversation=conversation,
                    state=state,
                    text=prompt,
                )
                _schedule_seller_followup(
                    db,
                    state=state,
                    conversation=conversation,
                    source_message_id=None,
                    step=1,
                )
            resumed += 1
            continue

        # Compatibilidade: fora do funil SDR IA, preserva o comportamento
        # completo da Fase 3 e apenas corrige estados móveis antigos em BILLING.
        if state.step != "BILLING":
            continue
        values = _mobile_values(collected)
        contract_type = str(values.get("tipo_contratacao") or "")
        if contract_type not in {"Número novo", "Número Portabilidade", "M2M"}:
            continue

        next_step = _mobile_next_step(values) or "MOBILE_PAYMENT_METHOD"
        _store_category_values(collected, values, mobile=True)
        _save_collected(state, collected)
        state.step = next_step
        state.last_error = ""

        conversation = db.get(Conversation, state.conversation_id)
        agent = db.get(AIAgentProfile, state.agent_id)
        if (
            conversation
            and agent
            and agent.active
            and agent.execution_mode == "AUTO"
            and conversation.status not in {"RESOLVIDO", "ENCERRADO", "BLOQUEADO"}
        ):
            prompt = _prompt_for_step(state, collected)
            _send_step_message(
                db,
                agent=agent,
                conversation=conversation,
                state=state,
                text=prompt,
            )
            _schedule_seller_followup(
                db,
                state=state,
                conversation=conversation,
                source_message_id=None,
                step=1,
            )
        resumed += 1

    if resumed:
        db.flush()
    return resumed

def _handoff_to_human(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
    agent: AIAgentProfile,
    reason: str,
) -> None:
    queue = queue_for_conversation(db, conversation.id)
    owner = assign_queue_user(db, queue) if queue else None
    if owner and owner.id == agent.user_id:
        owner = None
    conversation.assigned_user_id = owner.id if owner else None
    conversation.status = "EM_ATENDIMENTO" if owner else "EM_FILA"
    if conversation.lead:
        conversation.lead.assigned_user_id = owner.id if owner else conversation.lead.assigned_user_id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=owner.id if owner else None,
            assigned_by_id=agent.user_id,
            event="AI_CLOSER_HANDOFF_HUMAN",
        )
    )
    state.step = "ESCALATED"
    state.last_error = reason[:1000]
    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
    )
    if ai_state:
        ai_state.state = "HANDOFF_HUMAN"
        ai_state.handoff_at = utcnow()
    write_audit(
        db,
        user=agent.user,
        action="AI_CLOSER_HANDOFF_HUMAN",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"assigned_user_id": owner.id if owner else None, "reason": reason[:300]},
    )


def _conversation_history_rows(db: Session, conversation_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Message.direction, Message.body)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    ).all()
    return [{"direction": direction, "body": body} for direction, body in rows]


def _black_campaign_accepted(db: Session, conversation_id: int) -> bool:
    return black_campaign.black_accepted(_conversation_history_rows(db, conversation_id))


def _black_campaign_choice(db: Session, conversation_id: int) -> str:
    return black_campaign.black_choice(_conversation_history_rows(db, conversation_id))


def start_closing_from_sdr(
    db: Session,
    *,
    sdr_agent: AIAgentProfile,
    job: AIJob,
    decision: Any,
) -> bool:
    closer = active_closer(db)
    if not closer:
        return False
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.id == job.conversation_id)
        .with_for_update()
    )
    if not conversation:
        return False

    plan_label = str(
        getattr(decision, "offer_plan_code", None)
        or getattr(getattr(decision, "qualification", None), "desired_product", None)
        or ""
    ).strip()
    if plan_label.isdigit():
        plan_label += " Mega"
    if not plan_label:
        plan_label = "plano combinado"

    state = db.scalar(
        select(AIClosingState).where(AIClosingState.conversation_id == conversation.id)
    )
    created_state = state is None
    if not state:
        state = AIClosingState(
            conversation_id=conversation.id,
            agent_id=closer.id,
            external_id=f"prospecta-conversation-{conversation.id}",
            plan_label=plan_label,
            offer_price=_money(getattr(decision, "offer_price", None)),
            handoff_summary=str(getattr(decision, "summary", "") or ""),
        )
        db.add(state)
        db.flush()
    else:
        if state.step == "COMPLETED":
            return False
        state.agent_id = closer.id
        if not state.plan_label:
            state.plan_label = plan_label
        if state.offer_price is None:
            state.offer_price = _money(getattr(decision, "offer_price", None))
        latest_summary = str(getattr(decision, "summary", "") or "").strip()
        if latest_summary and latest_summary not in state.handoff_summary:
            state.handoff_summary = (
                f"{state.handoff_summary}\n{latest_summary}".strip()
            )[-5000:]

    black_combo = False
    if _black_campaign_accepted(db, conversation.id):
        state.handoff_summary = black_campaign.with_marker(state.handoff_summary)[-5000:]
        black_combo = _black_campaign_choice(db, conversation.id) == "COMBO"

    collected = _load_collected(state)
    if black_combo and "_black_combo" not in collected:
        collected["_black_combo"] = {"stage": black_campaign.COMBO_STAGE_FIBER}
    if state.step == "ESCALATED":
        resume_step = str(collected.pop("_resume_step", "") or "").strip()
        if not resume_step:
            # Escalonamento real para humano não deve ser reaberto automaticamente.
            return False
        state.step = resume_step
        state.last_error = ""
        _save_collected(state, collected)
    phone = _digits(conversation.contact.phone if conversation.contact else "")
    if phone:
        collected["phone"] = phone
    known_name = ""
    if conversation.lead and _looks_like_full_name(conversation.lead.contact_name or ""):
        known_name = conversation.lead.contact_name.strip()
    elif conversation.contact and _looks_like_full_name(conversation.contact.name or ""):
        known_name = conversation.contact.name.strip()
    if known_name:
        collected["name"] = known_name
    collected["handoffReason"] = str(getattr(decision, "handoff_reason", "") or "")
    collected["freeMonths"] = max(
        0,
        min(3, int(getattr(decision, "free_months", 0) or 0)),
    )
    collected["concessionLevel"] = max(
        0,
        min(3, int(getattr(decision, "concession_level", 0) or 0)),
    )
    _save_collected(state, collected)

    if created_state:
        state.step = "CPF" if collected.get("name") else "NAME"
    conversation.assigned_user_id = closer.user_id
    conversation.status = "EM_ATENDIMENTO"
    if conversation.lead:
        conversation.lead.assigned_user_id = closer.user_id
        if conversation.lead.stage not in {"GANHO", "NAO_CONTATAR"}:
            conversation.lead.stage = "NEGOCIACAO"
            conversation.lead.priority = "ALTA"

    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == conversation.id)
    )
    already_closing = bool(
        not created_state
        and ai_state
        and ai_state.state == "CLOSING"
        and conversation.assigned_user_id == closer.user_id
    )
    if not already_closing:
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=closer.user_id,
                assigned_by_id=sdr_agent.user_id,
                event="AI_HANDOFF_TO_CLOSER",
            )
        )
    if not ai_state:
        ai_state = AIConversationState(
            conversation_id=conversation.id,
            agent_id=closer.id,
            state="CLOSING",
            summary=state.handoff_summary,
            qualification_score=100,
            temperature="QUENTE",
            last_intent="PURCHASE_INTENT",
            handoff_at=utcnow(),
        )
        db.add(ai_state)
    else:
        ai_state.agent_id = closer.id
        ai_state.state = "CLOSING"
        ai_state.handoff_at = utcnow()
        ai_state.last_intent = "PURCHASE_INTENT"

    # A propriedade muda antes de qualquer mensagem do closer. A partir daqui,
    # qualquer envio do SDR/follow-up antigo é rejeitado pelos gates de ownership.
    db.flush()

    from app.services.ai_followups import cancel_pending_followups
    cancel_pending_followups(db, conversation.id, reason="HANDOFF_VENDEDOR_IA")

    if already_closing:
        # O fechamento já estava em andamento: não reinicia, não repete a pergunta.
        _schedule_seller_followup(
            db,
            state=state,
            conversation=conversation,
            source_message_id=job.message_id,
            step=1,
        )
        return True

    prompt = ""
    try:
        _create_crm_if_ready(db, state=state, conversation=conversation)
        if state.opportunity_id:
            playbook_client = SulnetCRMClient.from_settings()
            prompt = _advance_with_playbook(
                playbook_client,
                state,
                collected,
                fallback_step=state.step,
            )
            _save_collected(state, collected)
    except CRMIntegrationError as exc:
        state.last_error = str(exc)
        LOG.warning("closer_crm_create_deferred conversation=%s error=%s", conversation.id, exc)

    if not prompt:
        prompt = _prompt_for_step(state, collected)
    try:
        _send_step_message(
            db,
            agent=closer,
            conversation=conversation,
            state=state,
            text=prompt,
        )
    except GuardianBlockedError as exc:
        # A conversa já é do Vendedor IA e o job do SDR não volta mais: se a
        # primeira pergunta não sair, o cliente fica parado depois do "Quero
        # contratar". A pergunta é um texto fixo da etapa (não gerado pelo
        # modelo), então segue sem a revisão e o bloqueio fica registrado.
        LOG.warning(
            "closer_first_prompt_guardian_override conversation=%s",
            conversation.id,
        )
        _send_fixed_step_prompt(db, agent=closer, conversation=conversation, text=prompt)
        write_audit(
            db,
            user=closer.user,
            action="AI_CLOSER_FIRST_PROMPT_OVERRIDE",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={"step": state.step, "issues": list(exc.result.issue_codes)},
        )
    _schedule_seller_followup(
        db,
        state=state,
        conversation=conversation,
        source_message_id=job.message_id,
        step=1,
    )
    _record_activity(
        db,
        conversation=conversation,
        agent=closer,
        outcome="FECHAMENTO_INICIADO",
        description=f"Handoff SDR -> Vendedor IA. {state.handoff_summary}",
    )
    write_audit(
        db,
        user=closer.user,
        action="AI_CLOSER_STARTED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "sdr_agent_id": sdr_agent.id,
            "opportunity_id": state.opportunity_id,
            "plan": state.plan_label,
        },
    )
    return True


def _finish_run(
    db: Session,
    *,
    run: AIRun,
    job: AIJob,
    state: AIClosingState,
    reply: str,
    output: dict[str, Any],
    status: str = "CONCLUIDO",
) -> None:
    run.status = status
    run.intent = "CLOSING"
    run.qualification_score = 100
    run.temperature = "QUENTE"
    run.reply_text = reply
    run.summary = f"Vendedor IA · etapa {state.step}"
    run.output_json = json.dumps(output, ensure_ascii=False, default=str)
    run.finished_at = utcnow()
    job.status = "CONCLUIDO" if status == "CONCLUIDO" else "FALHOU"
    ai_state = db.scalar(
        select(AIConversationState).where(AIConversationState.conversation_id == job.conversation_id)
    )
    if ai_state:
        ai_state.agent_id = state.agent_id
        ai_state.state = "CLOSING" if state.step not in {"COMPLETED", "ESCALATED"} else state.step
        ai_state.last_run_id = run.id
        ai_state.updated_at = utcnow()


def _resolve_plan_value_field(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
    text: str,
) -> str:
    """Valor/produto do plano vêm do plano escolhido, nunca do texto livre.

    Em 05/11 (conversa 546) o CRM perguntou "Confirme o valor mensal do plano",
    o cliente respondeu "15GB" e o PATCH de monthlyValue="15GB" devolveu 500
    em ciclo. Se o cliente citar outro plano móvel, a escolha é trocada.
    """
    choice = black_campaign.mobile_choice_from_text(text) if _is_mobile_plan(state, collected) else None
    if choice and choice["name"] != state.plan_label:
        state.plan_label = choice["name"]
        state.offer_price = _money(choice["price"])
        for key in ("planId", "categoryId", "catalogPlanName", "contractProductName", "_catalog_plan_synced"):
            collected.pop(key, None)
    _sync_plan_context(client, state, collected)
    payload: dict[str, Any] = {}
    if state.offer_price is not None:
        payload["monthlyValue"] = _money_for_api(state.offer_price)
    if collected.get("planId"):
        payload["planId"] = collected["planId"]
        payload["planLabel"] = collected.get("catalogPlanName") or state.plan_label
    if payload and state.opportunity_id:
        client.update_opportunity(state.opportunity_id, payload)
    for key in (
        "_playbook_dynamic_code",
        "_playbook_dynamic_patch_key",
        "_playbook_dynamic_label",
        "_playbook_dynamic_options",
    ):
        collected.pop(key, None)
    _save_collected(state, collected)
    return _advance_with_playbook(
        client,
        state,
        collected,
        fallback_step="WAITING_CRM_READY",
    )


def process_seller_job(db: Session, job: AIJob, agent: AIAgentProfile) -> str:
    conversation = db.get(Conversation, job.conversation_id)
    message = db.get(Message, job.message_id)
    state = db.scalar(
        select(AIClosingState).where(AIClosingState.conversation_id == job.conversation_id)
    )
    if not conversation or not message or not state:
        job.status = "IGNORADO"
        job.error = "Estado do fechamento não encontrado."
        return "skipped"
    if state.step in {"COMPLETED", "ESCALATED"}:
        job.status = "IGNORADO"
        return "skipped"

    run = AIRun(
        job_id=job.id,
        agent_id=agent.id,
        conversation_id=job.conversation_id,
        message_id=job.message_id,
        execution_mode="AUTO" if seller_operational() else "SHADOW",
        model=agent.model,
        status="PROCESSANDO",
        input_snapshot_json=json.dumps(
            {"step": state.step, "message": _redact(message.body), "opportunity_id": state.opportunity_id},
            ensure_ascii=False,
        ),
        started_at=utcnow(),
    )
    db.add(run)
    db.flush()

    if not seller_operational() or agent.execution_mode != "AUTO":
        _finish_run(
            db, run=run, job=job, state=state, reply="",
            output={"reason": "seller_not_operational"}, status="FALHOU",
        )
        job.error = "Vendedor IA não está operacional."
        return "configuration"

    collected = _load_collected(state)
    text = (message.body or "").strip()
    reply = ""
    output: dict[str, Any] = {"step_before": state.step}
    client = SulnetCRMClient.from_settings()
    state_snapshot = {
        "step": state.step,
        "collected_json": state.collected_json,
        "plan_label": state.plan_label,
        "offer_price": state.offer_price,
        "credit_status": state.credit_status,
        "credit_analysis_id": state.credit_analysis_id,
        "contract_status": state.contract_status,
        "sign_url": state.sign_url,
        "opportunity_id": state.opportunity_id,
        "protocol": state.protocol,
    }

    try:
        contract_knowledge_reply = _seller_contract_knowledge_reply(
            state,
            collected,
            text,
        )
        if contract_knowledge_reply:
            reply = contract_knowledge_reply

        elif _seller_existing_customer_plan_change_requested(
            db,
            conversation.id,
            text,
        ):
            state.step = "COMPLETED"
            state.completed_at = utcnow()
            collected["_service_redirect"] = "EXISTING_CUSTOMER_PLAN_CHANGE"
            reply = (
                "Entendi! Como você já é cliente e quer aumentar ou trocar o seu plano atual, "
                f"essa alteração é feita pelo nosso atendimento. Entre em contato pelo {PLAN_CHANGE_SERVICE_PHONE} "
                "que a equipe consegue verificar as opções para sua conexão."
            )

        elif (
            seller_question_reply := _seller_question_reply(
                db,
                agent=agent,
                state=state,
                collected=collected,
                text=text,
            )
        ):
            reply = seller_question_reply

        elif _sdr_ia_mode(collected) and (
            state.step in SDR_IA_FORBIDDEN_STEPS or state.step == "BILLING"
        ):
            values = _category_values(collected)
            if _is_mobile_plan(state, collected):
                fallback_step = _mobile_next_step(
                    values,
                    sdr_ia_funnel=True,
                ) or "WAITING_CRM_READY"
            else:
                fallback_step = _billing_next_step(
                    values,
                    sdr_ia_funnel=True,
                ) or "WAITING_CRM_READY"
            playbook_step, playbook_question = _query_playbook_guidance(
                client,
                state,
                collected,
                fallback_step=fallback_step,
            )
            if playbook_step == "__FINISH__":
                reply = _finish_sale_contract(client, state, collected)
            else:
                state.step = playbook_step
                reply = playbook_question or _prompt_for_step(state, collected)

        elif state.step == COMBO_MOBILE_STEP:
            reply = _combo_choose_mobile(state, collected, text)
            if state.step == "COMPLETED" and conversation.lead:
                conversation.lead.stage = "GANHO"
                conversation.lead.next_action_at = None

        elif state.step == "NAME":
            name = _extract_name(text)
            if not name:
                reply = "Para emitir o contrato corretamente, preciso do seu nome completo. Pode me informar, por favor?"
            else:
                collected["name"] = name
                if conversation.contact:
                    conversation.contact.name = name
                if conversation.lead:
                    conversation.lead.contact_name = name
                _save_collected(state, collected)
                _create_crm_if_ready(db, state=state, conversation=conversation)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="CPF",
                )

        elif state.step == "CPF":
            cpf = _extract_cpf(text)
            if not cpf:
                reply = "Preciso do CPF com 11 dígitos para fazer a consulta cadastral. Pode me enviar?"
            else:
                if not state.opportunity_id:
                    _create_crm_if_ready(db, state=state, conversation=conversation)
                if not state.opportunity_id:
                    raise CRMIntegrationError("A oportunidade ainda não pôde ser criada no CRM.")
                collected["cpf"] = cpf
                try:
                    credit = client.consult_credit(state.opportunity_id, cpf)
                except CRMIntegrationError as exc:
                    error_key = _norm(str(exc))
                    if "nascimento" in error_key or "birth" in error_key:
                        state.step = "BIRTH_DATE"
                        reply = _prompt_for_step(state, collected)
                        credit = None
                    else:
                        raise
                if credit is not None:
                    state.credit_status = str(credit.get("status") or "")
                    state.credit_analysis_id = int(credit.get("analysisId") or 0) or None
                    collected["creditStatus"] = state.credit_status
                    _save_collected(state, collected)
                    if credit.get("approved"):
                        reply = _advance_with_playbook(
                            client,
                            state,
                            collected,
                            fallback_step="CEP",
                        )
                    elif credit.get("rejected"):
                        state.step = "CREDIT_REJECTED"
                        reply = _prompt_for_step(state, collected)
                    else:
                        state.step = "CREDIT_REVIEW"
                        reply = "Sua consulta ficou em análise. Vou manter a contratação aberta e sigo assim que o CRM liberar o resultado."

        elif state.step == "BIRTH_DATE":
            birth_date = _extract_birth_date(text)
            if not birth_date:
                reply = "Preciso da data de nascimento no formato DD/MM/AAAA para concluir a consulta cadastral."
            else:
                credit = client.consult_credit(
                    state.opportunity_id or 0,
                    birth_date=birth_date,
                )
                state.credit_status = str(credit.get("status") or "")
                state.credit_analysis_id = int(credit.get("analysisId") or 0) or None
                collected["creditStatus"] = state.credit_status
                _save_collected(state, collected)
                if credit.get("approved"):
                    reply = _advance_with_playbook(
                        client,
                        state,
                        collected,
                        fallback_step="CEP",
                    )
                elif credit.get("rejected"):
                    state.step = "CREDIT_REJECTED"
                    reply = _prompt_for_step(state, collected)
                else:
                    state.step = "CREDIT_REVIEW"
                    reply = "Sua consulta ficou em análise. Vou manter a contratação aberta e sigo assim que o CRM liberar o resultado."

        elif state.step == "CREDIT_REVIEW":
            status = client.get_status(state.opportunity_id or 0)
            credit_status = str((status.get("opportunity") or {}).get("creditStatus") or "")
            state.credit_status = credit_status
            n = _norm(credit_status)
            if any(token in n for token in ["aprovado", "pre-aprovado", "liberado"]):
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="CEP",
                )
            elif "reprov" in n:
                state.step = "CREDIT_REJECTED"
                reply = _prompt_for_step(state, collected)
            else:
                reply = "A análise cadastral ainda está em andamento. Assim que houver decisão, eu continuo a contratação."

        elif state.step == "CREDIT_REJECTED":
            normalized_text = _norm(text)
            new_cpf = _extract_cpf(text)

            if new_cpf:
                _clear_rejected_credit(state, collected)
                collected["cpf"] = new_cpf
                credit = client.consult_credit(state.opportunity_id or 0, new_cpf)
                state.credit_status = str(credit.get("status") or "")
                state.credit_analysis_id = int(credit.get("analysisId") or 0) or None
                collected["creditStatus"] = state.credit_status
                _save_collected(state, collected)

                if credit.get("approved"):
                    reply = _advance_with_playbook(
                        client,
                        state,
                        collected,
                        fallback_step="CEP",
                    )
                elif credit.get("rejected"):
                    state.step = "CREDIT_REJECTED"
                    reply = "Esse novo CPF também não foi aprovado. Se quiser, podemos tentar com outro CPF."
                else:
                    state.step = "CREDIT_REVIEW"
                    reply = "A consulta deste novo CPF ficou em análise. Vou manter a contratação aberta e sigo assim que houver retorno."

            elif (
                _wants_other_cpf(text)
                or normalized_text in {"outro cpf", "tentar outro cpf", "novo cpf", "outro_cpf"}
            ):
                _clear_rejected_credit(state, collected)
                state.step = "CPF"
                _save_collected(state, collected)
                reply = "Claro. Qual é o novo CPF que você quer usar para a contratação?"

            elif normalized_text in {"encerrar", "nao", "não"} or _yes_no(text) == "NAO":
                state.step = "COMPLETED"
                state.completed_at = utcnow()
                reply = "Certo. Encerramos esta tentativa de contratação. Se quiser tentar novamente depois, é só me chamar."

            else:
                interpreted = _interpret_ambiguous(
                    db,
                    agent=agent,
                    step=state.step,
                    message=text,
                    plan_label=state.plan_label,
                )
                if interpreted.human_requested or interpreted.wants_contest:
                    _handoff_to_human(
                        db,
                        state=state,
                        conversation=conversation,
                        agent=agent,
                        reason="Cliente solicitou análise/contestação após reprovação de crédito.",
                    )
                    reply = "Certo. Encaminhei seu caso para análise de um atendente, com todo o histórico da contratação."
                else:
                    reply = _prompt_for_step(state, collected)

        elif state.step == "CEP":
            cep = _extract_cep(text)
            if not cep:
                reply = "Pode me informar o CEP com 8 dígitos do endereço de instalação?"
            else:
                address = client.lookup_cep(cep)
                collected.update({
                    "cep": address.get("cep") or cep,
                    "street": address.get("street") or "",
                    "neighborhood": address.get("neighborhood") or "",
                    "city": address.get("city") or "",
                    "uf": address.get("uf") or "",
                })
                client.update_opportunity(state.opportunity_id or 0, {
                    "cep": collected["cep"],
                    "street": collected["street"],
                    "neighborhood": collected["neighborhood"],
                    "city": collected["city"],
                    "uf": collected["uf"],
                })
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="ADDRESS_NUMBER",
                )

        elif state.step == "ADDRESS_DETAILS":
            playbook_code = _playbook_code_key(
                str(collected.get("_playbook_code") or "")
            )
            value = re.sub(r"\s+", " ", text).strip(" .,!;:")
            if len(value) < 2:
                reply = _prompt_for_step(state, collected)
            elif playbook_code in {"street", "rua"}:
                value = re.sub(
                    r"^(rua|avenida|av\.?|travessa|estrada)\s*[:\-]?\s*",
                    "",
                    value,
                    flags=re.I,
                ).strip() or value
                collected["street"] = value[:180]
                client.update_opportunity(
                    state.opportunity_id or 0,
                    {"street": collected["street"]},
                )
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="ADDRESS_NUMBER",
                )
            elif playbook_code in {"neighborhood", "bairro"}:
                value = re.sub(
                    r"^bairro\s*[:\-]?\s*",
                    "",
                    value,
                    flags=re.I,
                ).strip() or value
                collected["neighborhood"] = value[:160]
                client.update_opportunity(
                    state.opportunity_id or 0,
                    {"neighborhood": collected["neighborhood"]},
                )
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="ADDRESS_NUMBER",
                )
            else:
                collected["_playbook_unmapped_code"] = str(
                    collected.get("_playbook_code") or ""
                )
                state.step = "ADDRESS_NUMBER"
                reply = _prompt_for_step(state, collected)

        elif state.step == "ADDRESS_NUMBER":
            number, complement = _extract_address_number(text)
            if not number:
                reply = "Qual é o número do imóvel? Se for apartamento, bloco, sala ou fundos, pode informar junto."
            else:
                collected["number"] = number
                if complement:
                    collected["complement"] = complement
                client.update_opportunity(state.opportunity_id or 0, {
                    "addressNumber": number,
                    "complement": complement,
                })
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="PLAN_CONFIRM",
                )

        elif state.step == "PLAN_CONFIRM":
            answer = _yes_no(text)
            interpreted = SellerInterpretation()
            if answer == "NAO_INFORMADO" or answer == "NAO":
                interpreted = _interpret_ambiguous(
                    db,
                    agent=agent,
                    step=state.step,
                    message=text,
                    plan_label=state.plan_label,
                )
                if answer == "NAO_INFORMADO":
                    answer = interpreted.confirmation
            if interpreted.human_requested:
                _handoff_to_human(
                    db,
                    state=state,
                    conversation=conversation,
                    agent=agent,
                    reason="Cliente pediu atendente durante confirmação do plano.",
                )
                reply = "Certo. Encaminhei a conversa para um atendente com todo o histórico."
            elif answer == "SIM":
                try:
                    client.update_opportunity(
                        state.opportunity_id or 0,
                        {
                            "planLabel": state.plan_label,
                            # Usado apenas para desempatar catálogo; o valor gravado
                            # continua sendo o preço oficial do produto no CRM.
                            "monthlyValue": _money_for_api(state.offer_price),
                        },
                    )
                except CRMIntegrationError:
                    products = client.products(state.plan_label)
                    if len(products) == 1:
                        client.update_opportunity(
                            state.opportunity_id or 0,
                            {
                                "planId": products[0].get("id"),
                                "planLabel": products[0].get("name"),
                            },
                        )
                        state.plan_label = str(products[0].get("name") or state.plan_label)
                    else:
                        raise
                _sync_plan_context(client, state, collected)
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="EMAIL",
                )
            elif answer == "NAO":
                requested = (interpreted.requested_plan or "").strip()
                if requested:
                    products = client.products(requested)
                    if len(products) == 1:
                        state.plan_label = str(products[0].get("name") or requested)
                        client.update_opportunity(
                            state.opportunity_id or 0,
                            {
                                "planId": products[0].get("id"),
                                "planLabel": state.plan_label,
                            },
                        )
                        _sync_plan_context(client, state, collected)
                        _save_collected(state, collected)
                        next_prompt = _advance_with_playbook(
                            client,
                            state,
                            collected,
                            fallback_step="EMAIL",
                        )
                        reply = f"Certo, alterei para {state.plan_label}. " + next_prompt
                    else:
                        reply = "Sem problema. Qual plano você quer contratar? Me diga a velocidade ou o nome do plano."
                else:
                    reply = "Sem problema. Qual plano você quer contratar? Me diga a velocidade ou o nome do plano."
            else:
                reply = _prompt_for_step(state, collected)

        elif state.step == "EMAIL":
            email = _extract_email(text)
            if not email:
                reply = "Preciso de um e-mail válido para concluir o cadastro. Pode me informar?"
            else:
                collected["email"] = email
                client.update_opportunity(
                    state.opportunity_id or 0,
                    {"clientEmail": email},
                )
                _sync_plan_context(client, state, collected)
                if _is_mobile_plan(state, collected):
                    values = _mobile_values(collected)
                    collected["mobileCategoryValues"] = values
                    _persist_mobile_category_form(client, state, collected)
                    fallback_step = _mobile_next_step(
                        values,
                        sdr_ia_funnel=_sdr_ia_mode(collected),
                    ) or "MOBILE_CONTRACT_TYPE"
                else:
                    fallback_step = (
                        "BILLING_PAYMENT_METHOD"
                        if _sdr_ia_mode(collected)
                        else "BILLING"
                    )
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step=fallback_step,
                )

        elif state.step == "PLAYBOOK_DYNAMIC_FIELD" and _playbook_code_key(
            str(collected.get("_playbook_dynamic_code") or collected.get("_playbook_code") or "")
        ) == "categoryid":
            # A categoria vem do plano escolhido, não de texto livre do cliente.
            # Gravar "Internet" como texto deixava o campo pendente no CRM e o
            # Vendedor pedia a mesma confirmação em ciclo (conversa 240).
            category_attempts = int(collected.get("_category_sync_attempts") or 0) + 1
            collected["_category_sync_attempts"] = category_attempts
            try:
                _persist_category_form(client, state, collected)
                category_saved = True
            except CRMIntegrationError:
                category_saved = False
            if category_saved:
                for key in (
                    "_playbook_dynamic_code",
                    "_playbook_dynamic_patch_key",
                    "_playbook_dynamic_label",
                    "_playbook_dynamic_options",
                ):
                    collected.pop(key, None)
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="WAITING_CRM_READY",
                )
            still_pending = state.step == "PLAYBOOK_DYNAMIC_FIELD" and _playbook_code_key(
                str(collected.get("_playbook_dynamic_code") or collected.get("_playbook_code") or "")
            ) == "categoryid"
            if not category_saved or (still_pending and category_attempts >= 2):
                _handoff_to_human(
                    db,
                    state=state,
                    conversation=conversation,
                    agent=agent,
                    reason="Categoria do produto não registrada no CRM após confirmação do cliente.",
                )
                reply = (
                    "Já registrei suas respostas. Para você não precisar repetir nada, "
                    "um consultor da Sulnet vai concluir este último registro com você por aqui."
                )

        elif state.step == "PLAYBOOK_DYNAMIC_FIELD" and _playbook_code_key(
            str(collected.get("_playbook_dynamic_code") or collected.get("_playbook_code") or "")
        ) in {"monthlyvalue", "productid", "planid"}:
            reply = _resolve_plan_value_field(client, state, collected, text)

        elif state.step == "PLAYBOOK_DYNAMIC_FIELD":
            value = _resolve_dynamic_playbook_value(text, collected)
            patch_key = str(
                collected.get("_playbook_dynamic_patch_key")
                or collected.get("_playbook_patch_key")
                or collected.get("_playbook_dynamic_code")
                or collected.get("_playbook_code")
                or ""
            ).strip()
            if value in (None, "", []):
                reply = _prompt_for_step(state, collected)
            elif not patch_key:
                state.step = "WAITING_CRM_READY"
                reply = (
                    "Recebi sua resposta. Estou validando este campo no CRM "
                    "para continuar a contratação."
                )
            else:
                client.update_playbook_field(
                    state.opportunity_id or 0,
                    patch_key=patch_key,
                    value=value,
                )
                collected.setdefault(
                    "dynamicPlaybookValues",
                    {},
                )[str(
                    collected.get("_playbook_dynamic_code")
                    or collected.get("_playbook_code")
                    or patch_key
                )] = value
                for key in (
                    "_playbook_dynamic_code",
                    "_playbook_dynamic_patch_key",
                    "_playbook_dynamic_label",
                    "_playbook_dynamic_options",
                ):
                    collected.pop(key, None)
                _save_collected(state, collected)
                reply = _advance_with_playbook(
                    client,
                    state,
                    collected,
                    fallback_step="WAITING_CRM_READY",
                )

        elif state.step.startswith("MOBILE_"):
            values = _mobile_values(collected)
            # Se o estado ficou atrás do que já está coletado (CRM rebobinou o
            # step), avança o cursor local antes de interpretar a mensagem.
            local_next = _mobile_next_step(
                values,
                sdr_ia_funnel=_sdr_ia_mode(collected),
            )
            if (
                local_next
                and local_next != state.step
                and _mobile_step_rank(local_next) > _mobile_step_rank(state.step)
            ):
                LOG.warning(
                    "seller_mobile_step_reconcile conversation=%s",
                    conversation.id,
                )
                state.step = local_next
            apply_step = _recover_mobile_step_for_text(
                state.step,
                text,
                values,
                sdr_ia_funnel=_sdr_ia_mode(collected),
            )
            if apply_step != state.step:
                LOG.info(
                    "seller_mobile_step_recovered conversation=%s",
                    conversation.id,
                )
                state.step = apply_step
            accepted, error_reply = _apply_mobile_answer(
                apply_step,
                text,
                values,
                sdr_ia_funnel=_sdr_ia_mode(collected),
            )
            if not accepted:
                # CRM rebobinou para tipo de contratação já respondido: avance
                # em vez de pedir Portabilidade/Sim de novo.
                if (
                    apply_step == "MOBILE_CONTRACT_TYPE"
                    and values.get("tipo_contratacao")
                ):
                    next_step = _mobile_next_step(
                        values,
                        sdr_ia_funnel=_sdr_ia_mode(collected),
                    )
                    if next_step and next_step != "MOBILE_CONTRACT_TYPE":
                        reply = _advance_with_playbook(
                            client,
                            state,
                            collected,
                            fallback_step=next_step,
                        )
                        _save_collected(state, collected)
                    else:
                        reply = error_reply
                else:
                    reply = error_reply
            else:
                collected["mobileCategoryValues"] = values
                current_code = str(collected.get("_playbook_code") or "")
                current_patch_key = str(
                    collected.get("_playbook_patch_key") or ""
                ).strip()
                current_step = str(collected.get("_playbook_step") or "")
                category_field = PLAYBOOK_CATEGORY_FIELD_BY_CODE.get(
                    _playbook_code_key(current_code)
                )
                if (
                    current_patch_key
                    and current_step == state.step
                    and category_field
                    and values.get(category_field) not in (None, "", [])
                ):
                    playbook_value = _current_playbook_canonical_value(
                        collected,
                        values[category_field],
                    )
                    # O formulário do CRM (category_form_seeds.php) só aceita
                    # "Número novo", "Número Portabilidade" ou "M2M"; com
                    # "Portabilidade" o contract/generate recusa o contrato.
                    if _playbook_code_key(current_code) == "tipocontratacao":
                        playbook_value = _crm_form_value("tipo_contratacao", playbook_value)
                    client.update_playbook_field(
                        state.opportunity_id or 0,
                        patch_key=current_patch_key,
                        value=playbook_value,
                    )
                else:
                    _persist_mobile_category_form(client, state, collected)
                next_step = _mobile_next_step(
                    values,
                    sdr_ia_funnel=_sdr_ia_mode(collected),
                )
                if next_step:
                    reply = _advance_with_playbook(
                        client,
                        state,
                        collected,
                        fallback_step=next_step,
                    )
                else:
                    missing = _mobile_missing_fields(
                        values,
                        sdr_ia_funnel=_sdr_ia_mode(collected),
                    )
                    if missing:
                        raise CRMIntegrationError(
                            "Checklist de Telefonia Móvel incompleto: " + ", ".join(missing)
                        )
                    client.update_opportunity(
                        state.opportunity_id or 0,
                        {
                            "paymentMethod": values["forma_pagamento"],
                            "invoiceDelivery": ", ".join(values.get("envio_fatura") or []),
                        },
                    )
                    _persist_mobile_category_form(client, state, collected)
                    playbook_step, playbook_question = _query_playbook_guidance(
                        client,
                        state,
                        collected,
                        fallback_step="__FINISH__",
                    )
                    if playbook_step == "__FINISH__":
                        reply = _finish_sale_contract(client, state, collected)
                    else:
                        state.step = playbook_step
                        reply = playbook_question or _prompt_for_step(state, collected)
                _save_collected(state, collected)

        elif state.step.startswith("BILLING_"):
            values = _category_values(collected)
            accepted, error_reply = _apply_billing_form_answer(
                state.step,
                text,
                values,
            )
            if not accepted:
                reply = error_reply
            else:
                current_code = str(collected.get("_playbook_code") or "")
                current_patch_key = str(
                    collected.get("_playbook_patch_key") or ""
                ).strip()
                current_step = str(collected.get("_playbook_step") or "")
                category_field = PLAYBOOK_CATEGORY_FIELD_BY_CODE.get(
                    _playbook_code_key(current_code)
                )
                if (
                    current_patch_key
                    and current_step == state.step
                    and category_field
                    and values.get(category_field) not in (None, "", [])
                ):
                    client.update_playbook_field(
                        state.opportunity_id or 0,
                        patch_key=current_patch_key,
                        value=_current_playbook_canonical_value(
                            collected,
                            values[category_field],
                        ),
                    )
                else:
                    _persist_category_form(
                        client,
                        state,
                        collected,
                        values,
                    )
                playbook_step, playbook_question = _query_playbook_guidance(
                    client,
                    state,
                    collected,
                    fallback_step="__FINISH__",
                )
                if playbook_step == "__FINISH__":
                    reply = _finish_sale_contract(client, state, collected)
                else:
                    state.step = playbook_step
                    reply = playbook_question or _prompt_for_step(state, collected)
                _save_collected(state, collected)

        elif state.step == "BILLING":
            billing = _billing_merge(collected, text)
            if not billing.get("paymentMethod") or not billing.get("dueDay"):
                interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
                if not billing.get("paymentMethod") and interpreted.payment_method:
                    billing["paymentMethod"] = interpreted.payment_method
                if not billing.get("dueDay") and interpreted.due_day:
                    billing["dueDay"] = interpreted.due_day
                if not billing.get("invoiceDelivery") and interpreted.invoice_delivery:
                    billing["invoiceDelivery"] = interpreted.invoice_delivery
            if not billing.get("paymentMethod") and not billing.get("dueDay"):
                reply = "Qual forma de pagamento você prefere: boleto ou débito em conta?"
            elif not billing.get("paymentMethod"):
                collected["dueDay"] = billing["dueDay"]
                _save_collected(state, collected)
                reply = "Perfeito. E qual forma de pagamento você prefere: boleto ou débito em conta?"
            elif not billing.get("dueDay"):
                collected["paymentMethod"] = billing["paymentMethod"]
                if billing.get("invoiceDelivery"):
                    collected["invoiceDelivery"] = billing["invoiceDelivery"]
                _save_collected(state, collected)
                reply = "Certo. Qual dia de vencimento você prefere?"
            else:
                collected.update(billing)
                client.update_opportunity(state.opportunity_id or 0, billing)
                _save_collected(state, collected)
                playbook_step, playbook_question = _query_playbook_guidance(
                    client,
                    state,
                    collected,
                    fallback_step="__FINISH__",
                )
                if playbook_step == "__FINISH__":
                    reply = _finish_sale_contract(client, state, collected)
                else:
                    state.step = playbook_step
                    reply = playbook_question or _prompt_for_step(state, collected)

        elif state.step == SALE_ACCEPTANCE_STEP:
            answer = _sale_acceptance_answer(text)
            interpreted = SellerInterpretation()
            if answer == "NAO_INFORMADO":
                interpreted = _interpret_ambiguous(
                    db,
                    agent=agent,
                    step=state.step,
                    message=text,
                    plan_label=state.plan_label,
                )
                answer = interpreted.confirmation
            if interpreted.human_requested:
                _handoff_to_human(
                    db,
                    state=state,
                    conversation=conversation,
                    agent=agent,
                    reason="Cliente pediu atendente no resumo da contratação.",
                )
                reply = "Certo. Encaminhei a conversa para um atendente com todo o histórico."
            elif answer == "SIM":
                collected["_sale_accepted"] = True
                collected["_sale_accepted_at"] = utcnow().isoformat()
                collected["_sale_accepted_message_id"] = message.id
                reply = _finish_sale_contract(client, state, collected)
            elif answer == "NAO":
                _handoff_to_human(
                    db,
                    state=state,
                    conversation=conversation,
                    agent=agent,
                    reason="Cliente não confirmou o resumo da contratação e quer ajustar.",
                )
                reply = (
                    "Sem problema, nada foi contratado. Um consultor da Sulnet vai "
                    "continuar com você por aqui para ajustar o que for preciso."
                )
            else:
                reply = _sale_acceptance_prompt(state, collected, reminder=True)

        elif state.step == "WAITING_CRM_READY":
            playbook_step, playbook_question = _query_playbook_guidance(
                client,
                state,
                collected,
                fallback_step="__FINISH__",
            )
            if playbook_step == "__FINISH__":
                reply = _finish_sale_contract(client, state, collected)
            elif playbook_step == "WAITING_CRM_READY":
                state.step = "WAITING_CRM_READY"
                reply = _prompt_for_step(state, collected)
            else:
                state.step = playbook_step
                reply = playbook_question or _prompt_for_step(state, collected)

        elif state.step in {"WAITING_CONTRACT", "WAITING_SIGNATURE"}:
            status = client.get_status(state.opportunity_id or 0)
            already_won = _complete_if_crm_already_won(
                client,
                state,
                collected,
                status_payload=status,
            )
            if already_won is not None:
                reply = _after_signed_text(state, collected)
                if reply == SIGNED_FINAL_MESSAGE and conversation.lead:
                    conversation.lead.stage = "GANHO"
                    conversation.lead.next_action_at = None
            else:
                contract = status.get("contract") or {}
                if client.supports("contract"):
                    detailed = client.get_contract(state.opportunity_id or 0)
                    if isinstance(detailed, dict) and detailed:
                        contract = {**contract, **detailed}

                contract_status = _contract_status(contract)
                sign_url = _contract_sign_url(contract) or state.sign_url
                if contract_status:
                    state.contract_status = contract_status

                if _contract_is_signed(state.contract_status):
                    _finalize_signed_sale(client, state, collected)
                    if state.step == "COMPLETED":
                        reply = _after_signed_text(state, collected)
                        if reply == SIGNED_FINAL_MESSAGE and conversation.lead:
                            conversation.lead.stage = "GANHO"
                            conversation.lead.next_action_at = None
                    else:
                        reply = (
                            "Recebi a confirmação da assinatura. "
                            "Estou concluindo o registro da sua contratação."
                        )
                elif sign_url:
                    state.sign_url = sign_url
                    state.step = "WAITING_SIGNATURE"
                    reply = f"Seu contrato está disponível para assinatura aqui: {sign_url}"
                elif state.step == "WAITING_CONTRACT":
                    reply = _prepare_contract_before_win(client, state, collected)
                else:
                    reply = _prompt_for_step(state, collected)

        else:
            reply = "Vou conferir o andamento da sua contratação."
            state.step = "WAITING_CONTRACT"

        _save_collected(state, collected)
        if reply:
            _send_step_message(
                db,
                agent=agent,
                conversation=conversation,
                state=state,
                text=reply,
                authoritative_contract_knowledge=bool(contract_knowledge_reply),
            )
            _schedule_seller_followup(
                db,
                state=state,
                conversation=conversation,
                source_message_id=message.id,
                step=1,
            )
            if collected.get("_service_redirect") == "EXISTING_CUSTOMER_PLAN_CHANGE":
                from app.services.ai_followups import cancel_pending_followups
                cancel_pending_followups(
                    db,
                    conversation.id,
                    reason="EXISTING_CUSTOMER_PLAN_CHANGE",
                )
                conversation.status = "RESOLVIDO"
                conversation.closed_at = utcnow()
        _record_activity(
            db,
            conversation=conversation,
            agent=agent,
            outcome=state.step,
            description=f"Vendedor IA avançou para {state.step}. Protocolo CRM: {state.protocol or '-'}",
        )
        output.update({
            "step_after": state.step,
            "opportunity_id": state.opportunity_id,
            "protocol": state.protocol,
            "credit_status": state.credit_status,
            "contract_status": state.contract_status,
            "playbook_mode": collected.get("_playbook_mode"),
            "playbook_ready_for_win": collected.get("_playbook_ready_for_win"),
            "playbook_missing_codes": collected.get("_playbook_missing_codes", []),
            "playbook_unmapped_code": collected.get("_playbook_unmapped_code"),
        })
        _finish_run(db, run=run, job=job, state=state, reply=reply, output=output)
        return "completed"
    except GuardianBlockedError as exc:
        state.step = str(state_snapshot["step"])
        state.collected_json = str(state_snapshot["collected_json"])
        state.plan_label = str(state_snapshot["plan_label"])
        state.offer_price = state_snapshot["offer_price"]
        state.credit_status = str(state_snapshot["credit_status"])
        state.credit_analysis_id = state_snapshot["credit_analysis_id"]
        state.contract_status = str(state_snapshot["contract_status"])
        state.sign_url = str(state_snapshot["sign_url"])
        state.opportunity_id = state_snapshot["opportunity_id"]
        state.protocol = str(state_snapshot["protocol"])
        state.last_error = str(exc)

        run.status = "FALHOU"
        run.error = str(exc)[:1200]
        run.finished_at = utcnow()
        job.status = "PENDENTE"
        job.error = str(exc)[:1200]
        job.locked_at = None
        job.available_at = utcnow() + timedelta(minutes=2)
        LOG.warning(
            "seller_guardian_blocked conversation=%s job=%s step=%s issues=%s",
            conversation.id,
            job.id,
            state.step,
            ",".join(exc.result.issue_codes) or "-",
        )
        return "guardian_blocked"
    except (CRMIntegrationError, ZApiError) as exc:
        # Nunca deixa uma falha transitória avançar o estado local parcialmente.
        # Assim a mesma resposta do cliente não é reinterpretada como resposta da etapa seguinte.
        state.step = str(state_snapshot["step"])
        state.collected_json = str(state_snapshot["collected_json"])
        state.plan_label = str(state_snapshot["plan_label"])
        state.offer_price = state_snapshot["offer_price"]
        state.credit_status = str(state_snapshot["credit_status"])
        state.credit_analysis_id = state_snapshot["credit_analysis_id"]
        state.contract_status = str(state_snapshot["contract_status"])
        state.sign_url = str(state_snapshot["sign_url"])
        state.opportunity_id = state_snapshot["opportunity_id"]
        state.protocol = str(state_snapshot["protocol"])
        state.last_error = str(exc)
        collected = _load_collected(state)

        run.status = "FALHOU"
        run.error = str(exc)[:1200]
        run.finished_at = utcnow()
        job.error = str(exc)[:1200]

        attempts = int(job.attempts or 0)
        kind = _closer_failure_kind(exc)
        crm_message = str(exc)[:300]
        if kind == "ownership":
            job.status = "IGNORADO"
            job.locked_at = None
            return "skipped"
        if kind == "contract" or (kind in {"config", "logic"} and attempts >= 1):
            # O CRM não vai gerar o contrato/aceitar a operação só com novas
            # tentativas (ex.: "Nenhum template de contrato para o produto").
            job.status = "CONCLUIDO"
            job.locked_at = None
            _escalate_closer_failure(
                db,
                state=state,
                conversation=conversation,
                agent=agent,
                reason=f"CRM recusou ({kind}) na etapa {state.step}: {crm_message}",
                notice=CONTRACT_HANDOFF_NOTICE if kind == "contract" else CLOSER_HANDOFF_NOTICE,
            )
            return "escalated"
        if kind == "data" and attempts >= 1:
            # Dado recusado pelo CRM: não repete o mesmo PATCH para sempre.
            errors = collected.setdefault("_data_errors", {})
            errors[state.step] = int(errors.get(state.step) or 0) + 1
            _save_collected(state, collected)
            job.status = "CONCLUIDO"
            job.locked_at = None
            if errors[state.step] >= 2:
                _escalate_closer_failure(
                    db,
                    state=state,
                    conversation=conversation,
                    agent=agent,
                    reason=f"CRM recusou o dado da etapa {state.step} duas vezes: {crm_message}",
                    notice=CLOSER_HANDOFF_NOTICE,
                )
                return "escalated"
            try:
                _send_step_message(
                    db,
                    agent=agent,
                    conversation=conversation,
                    state=state,
                    text=f"{DATA_RETRY_PREFIX} {_prompt_for_step(state, collected)}",
                )
            except Exception:
                LOG.exception("closer_data_retry_prompt_failed conversation=%s", conversation.id)
            LOG.warning(
                "closer_data_rejected conversation=%s job=%s error=%s",
                conversation.id,
                job.id,
                crm_message,
            )
            return "reasked"
        if attempts < 3:
            run.reply_text = ""
            job.status = "PENDENTE"
            job.available_at = utcnow() + timedelta(seconds=30 * max(1, attempts))
            LOG.warning(
                "seller_transient_retry conversation=%s job=%s step=%s attempt=%s error=%s",
                conversation.id,
                job.id,
                state.step,
                attempts,
                str(exc)[:300],
            )
            return "retry"

        # Depois das tentativas rápidas, mantém o fechamento com a IA e a etapa
        # exata salva. Falha técnica não deve jogar o cliente para outro fluxo.
        if not collected.get("_technical_notice_sent"):
            notice = (
                "Tive uma instabilidade momentânea no sistema, mas sua contratação "
                "continua salva exatamente de onde paramos. Vou seguir por aqui."
            )
            try:
                _send_text(db, agent=agent, conversation=conversation, text=notice)
                collected["_technical_notice_sent"] = True
                _save_collected(state, collected)
            except Exception:
                LOG.exception("seller_technical_notice_failed conversation=%s", conversation.id)

        cycles = int(collected.get("_retry_later_cycles") or 0) + 1
        collected["_retry_later_cycles"] = cycles
        _save_collected(state, collected)
        if cycles > 3:
            # ~15 minutos sem conseguir falar com o CRM: um consultor assume.
            job.status = "CONCLUIDO"
            job.locked_at = None
            _escalate_closer_failure(
                db,
                state=state,
                conversation=conversation,
                agent=agent,
                reason=f"CRM indisponível por mais de 15 minutos na etapa {state.step}: {crm_message}",
                notice=CLOSER_HANDOFF_NOTICE,
            )
            return "escalated"

        run.reply_text = ""
        job.status = "PENDENTE"
        job.attempts = 0
        job.locked_at = None
        job.available_at = utcnow() + timedelta(minutes=5)
        LOG.warning(
            "seller_retry_later conversation=%s job=%s step=%s error=%s",
            conversation.id,
            job.id,
            state.step,
            str(exc)[:300],
        )
        return "retry_later"
    except Exception as exc:
        LOG.exception("seller_job_failed job=%s", job.id)
        state.last_error = str(exc)[:1200]
        run.status = "FALHOU"
        run.error = str(exc)[:1200]
        run.finished_at = utcnow()
        job.status = "FALHOU"
        job.error = str(exc)[:1200]
        _escalate_closer_failure(
            db,
            state=state,
            conversation=conversation,
            agent=agent,
            reason=f"Falha inesperada no Vendedor IA: {type(exc).__name__}",
            notice=CLOSER_HANDOFF_NOTICE,
        )
        return "failed"


CLOSER_HANDOFF_NOTICE = (
    "Para não te deixar esperando, um consultor da Sulnet vai concluir sua contratação "
    "com você por aqui. Tudo o que você já informou está salvo."
)
CONTRACT_HANDOFF_NOTICE = (
    "Sua contratação já está registrada. Um consultor da Sulnet vai concluir a emissão "
    "do contrato e te enviar o link de assinatura por aqui."
)
DATA_RETRY_PREFIX = "Não consegui registrar essa informação no sistema."
_CLOSER_NOTICE_PREFIXES = (
    CLOSER_HANDOFF_NOTICE[:60],
    CONTRACT_HANDOFF_NOTICE[:60],
    DATA_RETRY_PREFIX,
    "Tive uma instabilidade momentânea no sistema",
)


def _closer_failure_kind(exc: Exception) -> str:
    """Classifica a falha para decidir entre tentar de novo, perguntar de novo ou passar a um consultor.

    - transient: CRM/WhatsApp fora do ar ou lento (rede, 429, 502-504, 52x, 500 em leitura);
    - contract: o CRM recusou gerar o contrato (template ausente, campos pendentes);
    - data: o CRM recusou um dado gravado (4xx, ou 500 em escrita);
    - config: credencial/permissão (401/403);
    - logic: regra interna do Vendedor IA (erro sem resposta HTTP);
    - ownership: a conversa não é mais do Vendedor IA (não se mexe nela).
    """
    if isinstance(exc, ZApiError):
        return "transient"
    if "propriedade exclusiva" in str(exc):
        # A conversa já é de um humano (ou de outro agente): não mexe nela.
        return "ownership"
    status = getattr(exc, "status_code", None)
    path = str(getattr(exc, "path", "") or "")
    method = str(getattr(exc, "method", "") or "").upper()
    code = str(getattr(exc, "error_code", "") or "")
    if code == "CRM_HTTP_ERROR" or status in {429, 502, 503, 504} or (status or 0) >= 520:
        return "transient"
    if "contract/generate" in path and status is not None and 400 <= status < 500:
        return "contract"
    if status in {401, 403}:
        return "config"
    if status is not None and 400 <= status < 500:
        return "data"
    if status == 500:
        return "data" if method in {"PATCH", "PUT"} else "transient"
    if status is None and not path:
        return "logic"
    return "transient"


def _escalate_closer_failure(
    db: Session,
    *,
    state: AIClosingState,
    conversation: Conversation,
    agent: AIAgentProfile,
    reason: str,
    notice: str,
) -> None:
    """Passa a venda a um consultor com o motivo e avisa o cliente: nunca fica em silêncio."""
    _handoff_to_human(db, state=state, conversation=conversation, agent=agent, reason=reason)
    _record_activity(
        db,
        conversation=conversation,
        agent=agent,
        outcome="VENDEDOR_IA_TRANSFERIU",
        description=(
            f"Vendedor IA transferiu a venda para um consultor. Motivo: {reason} "
            f"Oportunidade CRM: {state.opportunity_id or '-'}. Plano: {state.plan_label or '-'}."
        ),
    )
    LOG.warning(
        "closer_escalated conversation=%s opportunity=%s reason=%s",
        conversation.id,
        state.opportunity_id,
        reason[:200],
    )
    try:
        _send_text(db, agent=agent, conversation=conversation, text=notice, handoff_notice=True)
    except Exception:
        LOG.exception("closer_escalation_notice_failed conversation=%s", conversation.id)


def _deliver_signed_text(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    state: AIClosingState,
    text: str,
) -> None:
    if text == SIGNED_FINAL_MESSAGE:
        _send_text(db, agent=agent, conversation=conversation, text=text)
        if conversation.lead:
            conversation.lead.stage = "GANHO"
            conversation.lead.next_action_at = None
        return
    # Combo Black: a fibra foi assinada, segue a 2ª venda (móvel).
    _send_step_message(db, agent=agent, conversation=conversation, state=state, text=text)
    _schedule_seller_followup(db, state=state, conversation=conversation, source_message_id=None, step=1)


def poll_pending_contracts(db: Session, *, limit: int = 10) -> int:
    if not seller_operational():
        return 0
    states = list(
        db.scalars(
            select(AIClosingState).where(
                AIClosingState.step.in_(
                    [
                        "CREDIT_REVIEW",
                        "WAITING_CRM_READY",
                        "WAITING_CONTRACT",
                        "WAITING_SIGNATURE",
                    ]
                ),
                AIClosingState.updated_at <= utcnow() - timedelta(seconds=45),
            ).order_by(AIClosingState.updated_at.asc()).limit(limit)
        )
    )
    processed = 0
    client = SulnetCRMClient.from_settings()

    for state in states:
        if not state.opportunity_id:
            continue
        conversation = db.get(Conversation, state.conversation_id)
        agent = db.get(AIAgentProfile, state.agent_id)
        if (
            not conversation
            or not agent
            or not agent.active
            or agent.execution_mode != "AUTO"
        ):
            continue

        collected = _load_collected(state)
        step_before_poll = state.step
        collected_before_poll = state.collected_json
        try:
            if state.step == "CREDIT_REVIEW":
                status = client.get_status(state.opportunity_id)
                opportunity = status.get("opportunity") or {}
                credit_status = str(opportunity.get("creditStatus") or "")
                state.credit_status = credit_status
                normalized_credit = _norm(credit_status)

                if any(
                    token in normalized_credit
                    for token in ("aprovado", "pre-aprovado", "liberado")
                ):
                    prompt = _advance_with_playbook(
                        client,
                        state,
                        collected,
                        fallback_step="CEP",
                    )
                    _save_collected(state, collected)
                    _send_step_message(
                        db,
                        agent=agent,
                        conversation=conversation,
                        state=state,
                        text=prompt,
                    )
                    _schedule_seller_followup(
                        db,
                        state=state,
                        conversation=conversation,
                        source_message_id=None,
                        step=1,
                    )
                    processed += 1
                elif "reprov" in normalized_credit:
                    state.step = "CREDIT_REJECTED"
                    prompt = _prompt_for_step(state, collected)
                    _send_step_message(
                        db,
                        agent=agent,
                        conversation=conversation,
                        state=state,
                        text=prompt,
                    )
                    _schedule_seller_followup(
                        db,
                        state=state,
                        conversation=conversation,
                        source_message_id=None,
                        step=1,
                    )
                    processed += 1
                else:
                    state.updated_at = utcnow()
                continue

            if state.step == "WAITING_CRM_READY":
                playbook_step, playbook_question = _query_playbook_guidance(
                    client,
                    state,
                    collected,
                    fallback_step="__FINISH__",
                )
                if playbook_step == "__FINISH__":
                    reply = _finish_sale_contract(client, state, collected)
                    _save_collected(state, collected)
                    if state.step == "WAITING_SIGNATURE" and state.sign_url:
                        _send_text(
                            db,
                            agent=agent,
                            conversation=conversation,
                            text=reply,
                        )
                        _schedule_seller_followup(
                            db,
                            state=state,
                            conversation=conversation,
                            source_message_id=None,
                            step=1,
                        )
                        processed += 1
                    elif state.step == SALE_ACCEPTANCE_STEP:
                        _send_step_message(
                            db,
                            agent=agent,
                            conversation=conversation,
                            state=state,
                            text=reply,
                        )
                        _schedule_seller_followup(
                            db,
                            state=state,
                            conversation=conversation,
                            source_message_id=None,
                            step=1,
                        )
                        processed += 1
                    else:
                        state.updated_at = utcnow()
                    continue

                if playbook_step != "WAITING_CRM_READY":
                    state.step = playbook_step
                    prompt = playbook_question or _prompt_for_step(state, collected)
                    _save_collected(state, collected)
                    _send_step_message(
                        db,
                        agent=agent,
                        conversation=conversation,
                        state=state,
                        text=prompt,
                    )
                    _schedule_seller_followup(
                        db,
                        state=state,
                        conversation=conversation,
                        source_message_id=None,
                        step=1,
                    )
                    processed += 1
                else:
                    _save_collected(state, collected)
                    state.updated_at = utcnow()
                continue

            try:
                status = client.get_status(state.opportunity_id)
            except CRMIntegrationError as exc:
                # Depois da assinatura o CRM tira o lead da captação (404).
                # Com a assinatura confirmada pela ZapSign, segue sem o status.
                if exc.status_code != 404 or not _contract_is_signed(state.contract_status):
                    raise
                status = {}
            already_won = _complete_if_crm_already_won(
                client,
                state,
                collected,
                status_payload=status,
            )
            if already_won is not None:
                signed_text = _after_signed_text(state, collected)
                _save_collected(state, collected)
                _deliver_signed_text(
                    db,
                    agent=agent,
                    conversation=conversation,
                    state=state,
                    text=signed_text,
                )
                processed += 1
                continue

            contract = status.get("contract") or {}
            if client.supports("contract"):
                detailed = client.get_contract(state.opportunity_id)
                if isinstance(detailed, dict) and detailed:
                    contract = {**contract, **detailed}

            contract_status = _contract_status(contract)
            sign_url = _contract_sign_url(contract)
            if contract_status:
                state.contract_status = contract_status
            signed_paths = _signed_paths(status)
            if state.step == "WAITING_SIGNATURE":
                LOG.info(
                    "seller_signature_check state=%s opportunity_status=%s contract_status=%s signed_paths=%s keys=%s",
                    state.id,
                    _opportunity_status(status)[:30] or "-",
                    _norm(contract_status)[:40] or "-",
                    ",".join(signed_paths)[:300] or "-",
                    _payload_shape(status)[:400],
                )
            if signed_paths and not _contract_is_signed(state.contract_status):
                state.contract_status = "assinado"

            if _contract_is_signed(state.contract_status):
                _finalize_signed_sale(
                    client,
                    state,
                    collected,
                )
                if state.step != "COMPLETED":
                    # Assinatura confirmada: o cliente recebe a mensagem final
                    # já; o ganho no CRM segue pelo fluxo do próprio CRM/BKO.
                    collected["_completed_on_signature"] = True
                    state.step = "COMPLETED"
                    state.completed_at = utcnow()
                if state.step == "COMPLETED":
                    signed_text = _after_signed_text(state, collected)
                    _save_collected(state, collected)
                    _deliver_signed_text(
                        db,
                        agent=agent,
                        conversation=conversation,
                        state=state,
                        text=signed_text,
                    )
                    processed += 1
                else:
                    _save_collected(state, collected)
                    state.updated_at = utcnow()
                continue

            if sign_url:
                is_new_link = sign_url != state.sign_url
                state.sign_url = sign_url
                state.step = "WAITING_SIGNATURE"
                if is_new_link:
                    _send_text(
                        db,
                        agent=agent,
                        conversation=conversation,
                        text=(
                            "Seu contrato ficou pronto. "
                            f"Você pode assinar por este link: {sign_url}"
                        ),
                    )
                    _schedule_seller_followup(
                        db,
                        state=state,
                        conversation=conversation,
                        source_message_id=None,
                        step=1,
                    )
                    processed += 1
                else:
                    state.updated_at = utcnow()
                continue

            if state.step == "WAITING_CONTRACT":
                previous_url = state.sign_url
                _prepare_contract_before_win(client, state, collected)
                _save_collected(state, collected)
                if (
                    state.step == "WAITING_SIGNATURE"
                    and state.sign_url
                    and state.sign_url != previous_url
                ):
                    _send_text(
                        db,
                        agent=agent,
                        conversation=conversation,
                        text=(
                            "Seu contrato ficou pronto. "
                            f"Você pode assinar por este link: {state.sign_url}"
                        ),
                    )
                    _schedule_seller_followup(
                        db,
                        state=state,
                        conversation=conversation,
                        source_message_id=None,
                        step=1,
                    )
                    processed += 1
                else:
                    state.updated_at = utcnow()
            else:
                state.updated_at = utcnow()

        except GuardianBlockedError as exc:
            # Um bloqueio do Guardião em uma oportunidade não pode abortar o
            # poller inteiro. Reverte somente o estado local desta tentativa;
            # o CRM será consultado novamente no próximo ciclo.
            state.step = step_before_poll
            state.collected_json = collected_before_poll
            state.last_error = str(exc)[:1200]
            state.updated_at = utcnow()
            LOG.warning(
                "seller_contract_poll_guardian_blocked state=%s step=%s issues=%s",
                state.id,
                step_before_poll,
                ",".join(exc.result.issue_codes),
            )
        except CRMIntegrationError as exc:
            state.last_error = str(exc)[:1200]
            state.updated_at = utcnow()
            LOG.warning(
                "seller_contract_poll_failed state=%s error_code=%s",
                state.id,
                getattr(exc, "error_code", None) or type(exc).__name__,
            )
            # Assinatura pendente: o cliente já tem o link, só segue consultando.
            if step_before_poll in {"WAITING_CRM_READY", "WAITING_CONTRACT"}:
                kind = _closer_failure_kind(exc)
                poll_collected = _load_collected(state)
                failures = int(poll_collected.get("_poll_failures") or 0) + 1
                poll_collected["_poll_failures"] = failures
                _save_collected(state, poll_collected)
                if kind == "ownership":
                    pass
                elif kind == "contract" or (kind != "transient" and failures >= 3) or failures >= 20:
                    _escalate_closer_failure(
                        db,
                        state=state,
                        conversation=conversation,
                        agent=agent,
                        reason=f"Contrato não gerado pelo CRM ({kind}): {str(exc)[:300]}",
                        notice=CONTRACT_HANDOFF_NOTICE,
                    )
                    processed += 1

    return processed
