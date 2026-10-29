from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta
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
    AIRun,
    Conversation,
    ConversationAssignment,
    Lead,
    Message,
)
from app.services.audit import write_audit
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
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
MOBILE_CONTRACT_BUTTONS = [
    {"id": "portabilidade", "label": "Portabilidade"},
    {"id": "numero_novo", "label": "Número novo"},
]

SELLER_STEP_BUTTONS: dict[str, list[dict[str, str]]] = {
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
}
REMOVED_MOBILE_STEPS = {
    "MOBILE_NEW_ICCID",
    "MOBILE_NEW_NUMBER",
    "MOBILE_PORT_ICCID",
    "MOBILE_PORT_DATE",
    "MOBILE_M2M_QTY",
    "MOBILE_M2M_ICCIDS",
    "MOBILE_DUE_DAY",
    "MOBILE_ACTIVATION_FEE",
    "MOBILE_ACTIVATION_VALUE",
}


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
    if "port" in n:
        return "Número Portabilidade"
    if "numero_novo" in n or "novo" in n or "nova linha" in n or "linha nova" in n:
        return "Número novo"
    return None


def _extract_iccid(text: str) -> str | None:
    for candidate in re.findall(r"\d{18,22}", _digits(text)):
        if 18 <= len(candidate) <= 22:
            return candidate
    digits = _digits(text)
    return digits if 18 <= len(digits) <= 22 else None


def _extract_mobile_number(text: str) -> str | None:
    digits = _digits(text)
    return digits if 10 <= len(digits) <= 13 else None


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


def _mobile_values(collected: dict[str, Any]) -> dict[str, Any]:
    raw = collected.get("mobileCategoryValues")
    values = dict(raw) if isinstance(raw, dict) else {}
    values["tipo_assinatura_contrato"] = MOBILE_SIGNATURE_TYPE
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

    # O nome comercial usado no WhatsApp pode não ser o nome interno do ERP.
    # Ex.: "Sulmóvel 15GB" -> "Meu Trabalho Comigo 15GB".
    if not selected and state.plan_label:
        query = client.products(state.plan_label)
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

    # Repara oportunidades antigas que receberam o plano apenas pelo fallback
    # genérico e ainda não expõem product_id no endpoint de captura.
    if resolved_plan_id and plan_id != resolved_plan_id:
        client.update_opportunity(
            state.opportunity_id,
            {
                "planId": resolved_plan_id,
                "planLabel": collected["catalogPlanName"],
            },
        )


def _persist_mobile_category_form(
    client: SulnetCRMClient,
    state: AIClosingState,
    collected: dict[str, Any],
) -> None:
    if not state.opportunity_id:
        raise CRMIntegrationError("Oportunidade do CRM não disponível para gravar Telefonia Móvel.")
    values = _mobile_values(collected)
    collected["mobileCategoryValues"] = values
    category_id = int(collected.get("categoryId") or 0)
    plan_id = int(collected.get("planId") or 0)
    if not category_id or not plan_id:
        raise CRMIntegrationError(
            "Plano/categoria de Telefonia Móvel ainda não foi resolvido no catálogo do CRM."
        )
    client.update_category_form(
        state.opportunity_id,
        values,
        category_id=category_id,
        plan_id=plan_id,
    )


def _mobile_missing_fields(values: dict[str, Any]) -> list[str]:
    required = [
        "tipo_contratacao",
        "forma_pagamento",
        "unificar_faturas",
        "tipo_assinatura_contrato",
    ]
    contract_type = str(values.get("tipo_contratacao") or "")
    if contract_type == "Número Portabilidade":
        required += [
            "numero_portado",
            "port_titular",
            "port_nome_titular",
            "port_cpf_cnpj",
        ]
    elif contract_type not in {"Número novo", "Número Portabilidade"}:
        required.append("tipo_contratacao")

    if values.get("forma_pagamento") == "Boleto":
        required.append("envio_fatura")
    elif values.get("forma_pagamento") == "Débito em Conta":
        required += ["debito_banco", "debito_agencia", "debito_conta", "debito_titular"]
    if values.get("unificar_faturas") == "Não":
        required.append("unificar_motivo")

    missing: list[str] = []
    for field in required:
        value = values.get(field)
        if value in (None, "", []):
            missing.append(field)
    return list(dict.fromkeys(missing))


def _mobile_next_step(values: dict[str, Any]) -> str | None:
    contract_type = str(values.get("tipo_contratacao") or "")
    if contract_type not in {"Número novo", "Número Portabilidade"}:
        return "MOBILE_CONTRACT_TYPE"

    if contract_type == "Número Portabilidade":
        order = [
            ("numero_portado", "MOBILE_PORT_NUMBER"),
            ("port_titular", "MOBILE_PORT_TITULAR"),
            ("port_nome_titular", "MOBILE_PORT_NAME"),
            ("port_cpf_cnpj", "MOBILE_PORT_DOC"),
        ]
        for field, step in order:
            if not values.get(field):
                return step

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
    if not values.get("unificar_faturas"):
        return "MOBILE_UNIFY"
    if values.get("unificar_faturas") == "Não" and not values.get("unificar_motivo"):
        return "MOBILE_UNIFY_REASON"
    return None

def _apply_mobile_answer(
    step: str,
    text: str,
    values: dict[str, Any],
) -> tuple[bool, str]:
    if step == "MOBILE_CONTRACT_TYPE":
        contract_type = _extract_mobile_contract_type(text)
        if not contract_type:
            return False, "Escolha uma das opções: Portabilidade ou Número novo."
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


def _finish_sale_contract(client: SulnetCRMClient, state: AIClosingState) -> str:
    won = client.mark_won(state.opportunity_id or 0)
    contract = won.get("contract") or {}
    state.contract_status = str(contract.get("status") or "aguardando_backoffice")
    state.step = "WAITING_CONTRACT"
    try:
        started = client.start_contract(state.opportunity_id or 0)
    except CRMIntegrationError:
        started = contract
    sign_url = str(started.get("signUrl") or "").strip()
    state.contract_status = str(started.get("status") or state.contract_status)
    if sign_url:
        state.sign_url = sign_url
        state.step = "WAITING_SIGNATURE"
        return (
            "Seu cadastro foi concluído e o contrato já está pronto para assinatura. "
            f"Você pode assinar por este link: {sign_url}"
        )
    return _prompt_for_step(state, {})



def _seller_knowledge(db: Session, agent: AIAgentProfile) -> str:
    items = list(
        db.scalars(
            select(AIKnowledgeItem).where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.active.is_(True),
            ).order_by(AIKnowledgeItem.updated_at.desc()).limit(20)
        )
    )
    text = "\n".join(f"- {item.title}: {item.content}" for item in items)
    return text[:6000]


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
    from agents import Agent, Runner

    instructions = (agent.system_prompt or "").strip()
    knowledge = _seller_knowledge(db, agent)
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
        result = Runner.run_sync(interpreter, prompt)
        output = result.final_output
        return output if isinstance(output, SellerInterpretation) else SellerInterpretation.model_validate(output)
    except Exception:
        LOG.exception("seller_interpretation_failed step=%s", step)
        return SellerInterpretation()


def _send_text(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    text: str,
) -> Message:
    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    if not text.strip():
        raise ValueError("Mensagem vazia.")
    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone:
        raise CRMIntegrationError("Contato sem telefone para WhatsApp.")
    zapi = service_for_conversation(db, conversation.id)
    result = zapi.send_text(phone=phone, message=text)
    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    message = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=text,
        sender_user_id=agent.user_id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    db.flush()
    return message


def _seller_step_buttons(step: str, text: str = "") -> list[dict[str, str]]:
    buttons = SELLER_STEP_BUTTONS.get(step)
    if buttons:
        return [dict(item) for item in buttons]

    # Segurança adicional: se uma pergunta estruturada sair do mapa acima,
    # ainda tentamos transformar perguntas previsíveis (ex.: sim/não) em botões.
    from app.services.ai_quick_replies import infer_quick_replies

    inferred = infer_quick_replies(text)
    return inferred if len(inferred) >= 2 else []


def _send_step_message(
    db: Session,
    *,
    agent: AIAgentProfile,
    conversation: Conversation,
    state: AIClosingState,
    text: str,
) -> Message:
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
    if recent_duplicate:
        LOG.info(
            "seller_duplicate_suppressed conversation=%s step=%s message=%s",
            conversation.id,
            state.step,
            recent_duplicate.id,
        )
        return recent_duplicate

    buttons = _seller_step_buttons(state.step, text)
    if len(buttons) < 2:
        return _send_text(
            db,
            agent=agent,
            conversation=conversation,
            text=text,
        )

    from app.services.conversation_engine import register_message
    from app.services.zapi_instances import service_for_conversation

    phone = (conversation.contact.phone if conversation.contact else "").strip()
    if not phone:
        raise CRMIntegrationError("Contato sem telefone para WhatsApp.")
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
        "monthlyValue": state.offer_price,
        "sdrSummary": state.handoff_summary,
        "handoffReason": collected.get("handoffReason") or "",
        "runConsult": False,
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    result = client.create_opportunity(payload)
    state.opportunity_id = int(result.get("opportunityId") or 0) or None
    state.protocol = str(result.get("protocol") or "")
    if not state.opportunity_id:
        raise CRMIntegrationError("CRM não retornou o identificador da oportunidade.")


def _prompt_for_step(state: AIClosingState, collected: dict[str, Any]) -> str:
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
    if state.step == "MOBILE_PORT_NUMBER":
        return "Qual é o número que será portado?"
    if state.step == "MOBILE_PORT_TITULAR":
        return "A portabilidade está em nome do titular desta contratação? Responda sim ou não."
    if state.step == "MOBILE_PORT_NAME":
        return "Qual é o nome completo do titular da linha que será portada?"
    if state.step == "MOBILE_PORT_DOC":
        return "Qual é o CPF ou CNPJ do titular da linha que será portada?"
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
    if state.step == "BILLING":
        return "Agora falta o faturamento: como prefere pagar/receber a fatura e qual dia de vencimento você prefere?"
    if state.step == "CREDIT_REJECTED":
        return "A consulta cadastral não foi aprovada automaticamente. Se quiser, posso encaminhar para análise de um atendente."
    if state.step == "WAITING_CONTRACT":
        return "Seu cadastro comercial já foi concluído e o contrato está sendo preparado. Assim que o link de assinatura estiver disponível, eu envio por aqui."
    if state.step == "WAITING_SIGNATURE":
        return "Seu contrato já foi enviado. Assim que você concluir a assinatura, eu confirmo a contratação por aqui."
    return "Vou seguir com a próxima etapa da sua contratação."


SELLER_CUSTOMER_INPUT_STEPS = {
    "NAME",
    "CPF",
    "BIRTH_DATE",
    "CEP",
    "ADDRESS_NUMBER",
    "PLAN_CONFIRM",
    "EMAIL",
    "BILLING",
    "CREDIT_REJECTED",
    "MOBILE_CONTRACT_TYPE",
    "MOBILE_PORT_NUMBER",
    "MOBILE_PORT_TITULAR",
    "MOBILE_PORT_NAME",
    "MOBILE_PORT_DOC",
    "MOBILE_PAYMENT_METHOD",
    "MOBILE_INVOICE_DELIVERY",
    "MOBILE_DEBIT_BANK",
    "MOBILE_DEBIT_AGENCY",
    "MOBILE_DEBIT_ACCOUNT",
    "MOBILE_DEBIT_HOLDER",
    "MOBILE_UNIFY",
    "MOBILE_UNIFY_REASON",
}


def _seller_followup_prompt(
    state: AIClosingState,
    collected: dict[str, Any],
    *,
    followup_step: int,
) -> str:
    plan = state.plan_label or "plano combinado"
    prefix = (
        "Para continuarmos de onde paramos, "
        if followup_step <= 1
        else "Quando puder, seguimos exatamente de onde paramos: "
    )
    prompts = {
        "NAME": "me envie seu nome completo para eu iniciar o cadastro.",
        "CPF": "me envie seu CPF para eu continuar a consulta cadastral.",
        "BIRTH_DATE": "me envie sua data de nascimento no formato DD/MM/AAAA.",
        "CEP": "me envie o CEP do endereço para eu continuar a contratação.",
        "ADDRESS_NUMBER": "me informe o número do imóvel e, se houver, o complemento.",
        "EMAIL": "me informe o e-mail que ficará no cadastro.",
        "MOBILE_PORT_NUMBER": "me informe o número com DDD que será portado.",
        "MOBILE_PORT_NAME": "me informe o nome completo do titular da linha.",
        "MOBILE_PORT_DOC": "me informe o CPF ou CNPJ do titular da linha.",
        "MOBILE_DEBIT_BANK": "me informe o banco para o débito em conta.",
        "MOBILE_DEBIT_AGENCY": "me informe a agência.",
        "MOBILE_DEBIT_ACCOUNT": "me informe a conta.",
        "MOBILE_DEBIT_HOLDER": "me informe o nome do titular da conta bancária.",
        "MOBILE_UNIFY_REASON": "me informe o motivo para manter as faturas separadas.",
        "BILLING": "me informe a forma de pagamento/faturamento e o dia de vencimento.",
    }
    if state.step == "PLAN_CONFIRM":
        return f"Para seguirmos com a contratação do {plan}, confirma esse plano?"
    if state.step == "MOBILE_CONTRACT_TYPE":
        return "Para continuar com o Sulmóvel, escolha como deseja a linha:"
    if state.step == "MOBILE_PORT_TITULAR":
        return "Para continuar a portabilidade, a linha está no nome do titular desta contratação?"
    if state.step == "MOBILE_PAYMENT_METHOD":
        return "Para continuar, escolha a forma de pagamento:"
    if state.step == "MOBILE_INVOICE_DELIVERY":
        return "Para continuar, escolha como quer receber a fatura:"
    if state.step == "MOBILE_UNIFY":
        return "Para continuar, deseja unificar esta fatura com outras faturas Sulnet?"
    if state.step == "CREDIT_REJECTED":
        return "Se quiser encaminhar a análise cadastral para um atendente, escolha Sim. Caso contrário, pode responder Não."
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
    message = _send_step_message(
        db,
        agent=agent,
        conversation=conversation,
        state=state,
        text=text,
    )
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
            "buttons": len(_seller_step_buttons(state.step, text)),
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


def resume_simplified_mobile_states(db: Session, *, limit: int = 50) -> int:
    states = list(
        db.scalars(
            select(AIClosingState)
            .where(
                AIClosingState.updated_at >= utcnow() - timedelta(days=2),
            )
            .order_by(AIClosingState.updated_at.desc())
            .limit(limit)
        )
    )
    resumed = 0
    obsolete_keys = {
        "novo_iccid",
        "numero_novo",
        "port_iccid",
        "port_data",
        "m2m_quantidade",
        "m2m_iccids",
        "vencimento",
        "taxa_ativacao",
        "taxa_valor",
    }

    for state in states:
        collected = _load_collected(state)
        values = _mobile_values(collected)
        contract_type = str(values.get("tipo_contratacao") or "")
        is_mobile_flow = contract_type in {"Número novo", "Número Portabilidade", "M2M"}
        changed = state.step in REMOVED_MOBILE_STEPS

        if contract_type == "M2M":
            values.pop("tipo_contratacao", None)
            changed = True
            is_mobile_flow = True

        # Recuperações genéricas antigas podiam devolver o móvel para BILLING.
        # Corrige imediatamente para a próxima etapa específica do Sulmóvel.
        if state.step == "BILLING" and is_mobile_flow:
            changed = True

        if not changed:
            continue

        for key in obsolete_keys:
            values.pop(key, None)

        next_step = _mobile_next_step(values) or "MOBILE_PAYMENT_METHOD"
        collected["mobileCategoryValues"] = values
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
    conversation = db.get(Conversation, job.conversation_id)
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
            offer_price=getattr(decision, "offer_price", None),
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
            state.offer_price = getattr(decision, "offer_price", None)
        latest_summary = str(getattr(decision, "summary", "") or "").strip()
        if latest_summary and latest_summary not in state.handoff_summary:
            state.handoff_summary = (
                f"{state.handoff_summary}\n{latest_summary}".strip()
            )[-5000:]

    collected = _load_collected(state)
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

    try:
        _create_crm_if_ready(db, state=state, conversation=conversation)
    except CRMIntegrationError as exc:
        state.last_error = str(exc)
        LOG.warning("closer_crm_create_deferred conversation=%s error=%s", conversation.id, exc)

    prompt = _prompt_for_step(state, collected)
    _send_step_message(
        db,
        agent=closer,
        conversation=conversation,
        state=state,
        text=prompt,
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
        if state.step == "NAME":
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
                state.step = "CPF"
                reply = _prompt_for_step(state, collected)

        elif state.step == "CPF":
            cpf = _extract_cpf(text)
            if not cpf:
                reply = "Preciso do CPF com 11 dígitos para fazer a consulta cadastral. Pode me enviar?"
            else:
                if not state.opportunity_id:
                    _create_crm_if_ready(db, state=state, conversation=conversation)
                if not state.opportunity_id:
                    raise CRMIntegrationError("A oportunidade ainda não pôde ser criada no CRM.")
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
                        state.step = "CEP"
                        reply = _prompt_for_step(state, collected)
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
                    state.step = "CEP"
                    reply = _prompt_for_step(state, collected)
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
                state.step = "CEP"
                reply = _prompt_for_step(state, collected)
            elif "reprov" in n:
                state.step = "CREDIT_REJECTED"
                reply = _prompt_for_step(state, collected)
            else:
                reply = "A análise cadastral ainda está em andamento. Assim que houver decisão, eu continuo a contratação."

        elif state.step == "CREDIT_REJECTED":
            interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
            if interpreted.human_requested or interpreted.wants_contest or _yes_no(text) == "SIM":
                _handoff_to_human(
                    db, state=state, conversation=conversation, agent=agent,
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
                    "number": number,
                    "complement": complement,
                })
                _save_collected(state, collected)
                state.step = "PLAN_CONFIRM"
                reply = _prompt_for_step(state, collected)

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
                            "monthlyValue": state.offer_price,
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
                state.step = "EMAIL"
                reply = _prompt_for_step(state, collected)
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
                        state.step = "EMAIL"
                        reply = f"Certo, alterei para {state.plan_label}. " + _prompt_for_step(state, collected)
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
                    state.step = _mobile_next_step(values) or "MOBILE_CONTRACT_TYPE"
                else:
                    state.step = "BILLING"
                _save_collected(state, collected)
                reply = _prompt_for_step(state, collected)

        elif state.step.startswith("MOBILE_"):
            values = _mobile_values(collected)
            accepted, error_reply = _apply_mobile_answer(
                state.step,
                text,
                values,
            )
            if not accepted:
                reply = error_reply
            else:
                collected["mobileCategoryValues"] = values
                _persist_mobile_category_form(client, state, collected)
                next_step = _mobile_next_step(values)
                if next_step:
                    state.step = next_step
                    reply = _prompt_for_step(state, collected)
                else:
                    missing = _mobile_missing_fields(values)
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
                    reply = _finish_sale_contract(client, state)
                _save_collected(state, collected)

        elif state.step == "BILLING":
            billing = _billing_local(text)
            if not billing.get("paymentMethod") or not billing.get("dueDay"):
                interpreted = _interpret_ambiguous(db, agent=agent, step=state.step, message=text, plan_label=state.plan_label)
                if not billing.get("paymentMethod") and interpreted.payment_method:
                    billing["paymentMethod"] = interpreted.payment_method
                if not billing.get("dueDay") and interpreted.due_day:
                    billing["dueDay"] = interpreted.due_day
                if not billing.get("invoiceDelivery") and interpreted.invoice_delivery:
                    billing["invoiceDelivery"] = interpreted.invoice_delivery
            if not billing.get("paymentMethod") and not billing.get("dueDay"):
                reply = "Me informe a forma de pagamento/faturamento e o dia de vencimento. Ex.: boleto, dia 10."
            elif not billing.get("paymentMethod"):
                collected["dueDay"] = billing["dueDay"]
                _save_collected(state, collected)
                reply = "Perfeito. E qual forma de pagamento/faturamento você prefere?"
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
                reply = _finish_sale_contract(client, state)

        elif state.step in {"WAITING_CONTRACT", "WAITING_SIGNATURE"}:
            status = client.get_status(state.opportunity_id or 0)
            contract = status.get("contract") or {}
            contract_status = str(contract.get("status") or "")
            sign_url = str(contract.get("signUrl") or state.sign_url or "").strip()
            state.contract_status = contract_status
            if contract_status == "assinado":
                state.step = "COMPLETED"
                state.completed_at = utcnow()
                reply = (
                    "Contrato assinado com sucesso! Sua contratação está concluída. "
                    "A ativação segue para a operação da Sulnet, com previsão de até 48 horas."
                )
                if conversation.lead:
                    conversation.lead.stage = "GANHO"
                    conversation.lead.next_action_at = None
            elif sign_url:
                state.sign_url = sign_url
                state.step = "WAITING_SIGNATURE"
                reply = f"Seu contrato está disponível para assinatura aqui: {sign_url}"
            else:
                try:
                    started = client.start_contract(state.opportunity_id or 0)
                except CRMIntegrationError:
                    started = contract
                sign_url = str(started.get("signUrl") or "").strip()
                if sign_url:
                    state.sign_url = sign_url
                    state.step = "WAITING_SIGNATURE"
                    reply = f"Seu contrato ficou pronto. Você pode assinar por este link: {sign_url}"
                else:
                    state.step = "WAITING_CONTRACT"
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
            )
            _schedule_seller_followup(
                db,
                state=state,
                conversation=conversation,
                source_message_id=message.id,
                step=1,
            )
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
        })
        _finish_run(db, run=run, job=job, state=state, reply=reply, output=output)
        return "completed"
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
        _handoff_to_human(
            db, state=state, conversation=conversation, agent=agent,
            reason="Falha inesperada no Vendedor IA.",
        )
        return "failed"


def poll_pending_contracts(db: Session, *, limit: int = 10) -> int:
    if not seller_operational():
        return 0
    states = list(
        db.scalars(
            select(AIClosingState).where(
                AIClosingState.step.in_(["WAITING_CONTRACT", "WAITING_SIGNATURE"]),
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
        if not conversation or not agent or not agent.active or agent.execution_mode != "AUTO":
            continue
        try:
            status = client.get_status(state.opportunity_id)
            contract = status.get("contract") or {}
            contract_status = str(contract.get("status") or "")
            sign_url = str(contract.get("signUrl") or "").strip()
            state.contract_status = contract_status
            if contract_status == "assinado":
                state.step = "COMPLETED"
                state.completed_at = utcnow()
                _send_text(
                    db,
                    agent=agent,
                    conversation=conversation,
                    text=(
                        "Contrato assinado com sucesso! Sua contratação está concluída. "
                        "A ativação segue para a operação da Sulnet, com previsão de até 48 horas."
                    ),
                )
                if conversation.lead:
                    conversation.lead.stage = "GANHO"
                    conversation.lead.next_action_at = None
                processed += 1
                continue
            if not sign_url and state.step == "WAITING_CONTRACT":
                try:
                    started = client.start_contract(state.opportunity_id)
                    sign_url = str(started.get("signUrl") or "").strip()
                    contract_status = str(started.get("status") or contract_status)
                    state.contract_status = contract_status
                except CRMIntegrationError:
                    pass
            if sign_url and sign_url != state.sign_url:
                state.sign_url = sign_url
                state.step = "WAITING_SIGNATURE"
                _send_text(
                    db,
                    agent=agent,
                    conversation=conversation,
                    text=f"Seu contrato ficou pronto. Você pode assinar por este link: {sign_url}",
                )
                processed += 1
            else:
                state.updated_at = utcnow()
        except CRMIntegrationError as exc:
            state.last_error = str(exc)[:1200]
            state.updated_at = utcnow()
            LOG.warning("seller_contract_poll_failed state=%s error=%s", state.id, exc)
    return processed
