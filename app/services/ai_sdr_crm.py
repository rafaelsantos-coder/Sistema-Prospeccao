"""Ciclo de oportunidade CRM do SDR.

Regra de negócio (06/11/2026):
1. Toda resposta humana (não automática) do cliente abre uma oportunidade
   no CRM Sulnet com pelo menos nome + telefone.
2. Se a conversa for até a contratação, o Vendedor IA conduz o ganho
   (reutiliza a mesma oportunidade).
3. Se o cliente recusar / pedir para parar, a oportunidade vai a perda
   com o motivo certo mapeado.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Activity, Conversation, Lead, Message
from app.services.ai_sdr_models import AIDecision
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
from app.timeutils import utcnow


LOG = logging.getLogger("sulnet_ai_sdr_crm")

# Motivos de perda padronizados (código interno → rótulo CRM).
LOSS_REASONS: dict[str, str] = {
    "SEM_INTERESSE": "Sem interesse",
    "OPT_OUT": "Pediu para não receber mais contato",
    "NUMERO_ERRADO": "Número errado",
    "JA_E_CLIENTE": "Já é cliente Sulnet (sem nova contratação)",
    "PRECO": "Preço / condição comercial",
    "SEM_COBERTURA": "Sem cobertura na região",
    "CONCORRENTE": "Ficou com outro provedor",
    "SEM_RESPOSTA": "Sem resposta após follow-ups",
    "NAO_QUALIFICADO": "Não qualificado",
    "OUTRO": "Outro motivo",
}

_AUTO_REPLY_CUES = (
    "mensagem automatica",
    "resposta automatica",
    "mensagem de ausencia",
    "fora do horario",
    "fora do expediente",
    "estamos ausentes",
    "estamos fechados",
    "estamos em recesso",
    "horario de atendimento",
    "nosso horario de atendimento",
    "retornamos em breve",
    "retornaremos em breve",
    "retornamos assim que",
    "responderemos assim que",
    "sua mensagem foi recebida",
    "recebemos sua mensagem",
    "agradecemos o contato",
    "este e um numero comercial",
    "este e um atendimento automatico",
    "bot de atendimento",
    "assistente virtual",
    "nao estamos disponiveis no momento",
    "voltamos a responder",
    "proximo dia util",
)


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _digits(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def is_automatic_reply(body: str | None) -> bool:
    """Heurística PT-BR para saudação/ausência de WhatsApp Business e bots."""
    folded = _fold(body)
    if not folded:
        return False
    if any(cue in folded for cue in _AUTO_REPLY_CUES):
        return True
    # Templates longos de ausência costumam citar horário + retorno juntos.
    has_hours = any(
        token in folded
        for token in ("segunda a sexta", "seg a sex", "das 8", "das 9", "as 18", "as 17")
    )
    has_return = any(
        token in folded
        for token in ("retorn", "responderemos", "em breve", "assim que abrirmos")
    )
    return has_hours and has_return and len(folded) >= 80


def crm_configured() -> bool:
    return bool(settings.sulnet_crm_base_url and settings.sulnet_crm_bearer_token)


def _lead_payload(lead: Lead) -> dict[str, Any]:
    try:
        parsed = json.loads(lead.raw_data or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_lead_payload(lead: Lead, payload: dict[str, Any]) -> None:
    lead.raw_data = json.dumps(payload, ensure_ascii=False, default=str)


def get_crm_opportunity_meta(lead: Lead | None) -> dict[str, Any]:
    if not lead:
        return {}
    meta = _lead_payload(lead).get("crm_opportunity")
    return dict(meta) if isinstance(meta, dict) else {}


def set_crm_opportunity_meta(lead: Lead, **fields: Any) -> dict[str, Any]:
    payload = _lead_payload(lead)
    current = payload.get("crm_opportunity")
    meta = dict(current) if isinstance(current, dict) else {}
    meta.update({key: value for key, value in fields.items() if value is not None})
    payload["crm_opportunity"] = meta
    _save_lead_payload(lead, payload)
    return meta


def resolve_loss_reason(
    decision: AIDecision | None,
    *,
    latest_customer_message: str = "",
) -> tuple[str, str]:
    """Devolve (codigo, rotulo) para perda no CRM."""
    folded = _fold(latest_customer_message)
    intent = str(getattr(decision, "intent", "") or "").upper()
    objection = str(getattr(decision, "objection", "") or "").upper()
    sales_state = str(getattr(decision, "sales_state", "") or "").upper()

    if any(
        cue in folded
        for cue in (
            "numero errado",
            "pessoa errada",
            "engano",
            "nao e esse numero",
            "nao sou essa pessoa",
        )
    ):
        return "NUMERO_ERRADO", LOSS_REASONS["NUMERO_ERRADO"]

    if any(
        cue in folded
        for cue in (
            "ja sou assinante",
            "ja sou cliente",
            "sou assinante",
            "sou cliente",
            "ja tenho sulnet",
        )
    ) and any(
        cue in folded
        for cue in ("obrigado", "valeu", "nao quero", "nao preciso", "pode parar")
    ):
        return "JA_E_CLIENTE", LOSS_REASONS["JA_E_CLIENTE"]

    if intent == "DO_NOT_CONTACT" or sales_state == "OPT_OUT":
        if any(
            cue in folded
            for cue in (
                "pare",
                "parar",
                "pode parar",
                "nao me contate",
                "nao quero mais mensagens",
                "nao quero mais contato",
                "remover",
                "descadastrar",
                "sair da lista",
            )
        ):
            return "OPT_OUT", LOSS_REASONS["OPT_OUT"]
        return "SEM_INTERESSE", LOSS_REASONS["SEM_INTERESSE"]

    if objection == "PRICE" or "preco" in folded or "caro" in folded:
        return "PRECO", LOSS_REASONS["PRECO"]

    if "cobertura" in folded or "nao atende" in folded:
        return "SEM_COBERTURA", LOSS_REASONS["SEM_COBERTURA"]

    if objection in {"NO_INTEREST", "NOT_INTERESTED"} or intent == "NOT_INTERESTED":
        return "SEM_INTERESSE", LOSS_REASONS["SEM_INTERESSE"]

    return "OUTRO", LOSS_REASONS["OUTRO"]


def _opportunity_display_name(
    *,
    lead: Lead | None,
    conversation: Conversation,
) -> str:
    candidates = [
        (lead.contact_name if lead else "") or "",
        (lead.business_name if lead else "") or "",
        (conversation.contact.name if conversation.contact else "") or "",
        (conversation.contact.business_name if conversation.contact else "") or "",
    ]
    for item in candidates:
        clean = re.sub(r"\s+", " ", str(item).strip())
        if clean and clean.lower() not in {"whatsapp", "cliente", "contato"}:
            return clean[:150]
    phone = _digits(
        (lead.phone if lead else "")
        or (conversation.contact.phone if conversation.contact else "")
    )
    suffix = phone[-4:] if len(phone) >= 4 else phone or "novo"
    return f"Lead WhatsApp {suffix}"


def ensure_local_lead_for_conversation(
    db: Session,
    conversation: Conversation,
) -> Lead | None:
    """Garante um Lead local (prospecção SDR) ligado à conversa."""
    if conversation.lead_id:
        lead = db.get(Lead, conversation.lead_id)
        if lead:
            return lead

    contact = conversation.contact
    phone = _digits(contact.phone if contact else "")
    if not phone:
        return None

    from app.services.conversation_engine import find_lead_by_phone

    lead = find_lead_by_phone(db, phone)
    if lead:
        conversation.lead_id = lead.id
        if contact and not contact.lead_id:
            contact.lead_id = lead.id
        return lead

    name = _opportunity_display_name(lead=None, conversation=conversation)
    lead = Lead(
        business_name=name,
        contact_name=name,
        phone=phone,
        source="BASE_ATIVA",
        origin_channel="WHATSAPP",
        team="DIGITAL",
        stage="CONTATO_REALIZADO",
        temperature="MORNO",
        notes="Lead aberto automaticamente pelo SDR IA na primeira resposta humana.",
        raw_data="{}",
    )
    db.add(lead)
    db.flush()
    conversation.lead_id = lead.id
    if contact:
        contact.lead_id = lead.id
    return lead


def _record_activity(
    db: Session,
    *,
    lead: Lead,
    agent_user_id: int | None,
    outcome: str,
    description: str,
) -> None:
    db.add(
        Activity(
            lead_id=lead.id,
            user_id=agent_user_id,
            kind="WHATSAPP",
            status="CONCLUIDA",
            channel="WHATSAPP",
            outcome=outcome,
            description=description,
            completed_at=utcnow(),
        )
    )


def ensure_sdr_crm_opportunity(
    db: Session,
    *,
    conversation: Conversation,
    latest_message: Message | None,
    agent_user_id: int | None = None,
) -> dict[str, Any] | None:
    """Abre oportunidade no CRM na 1ª resposta humana (nome + telefone)."""
    body = latest_message.body if latest_message else ""
    if is_automatic_reply(body):
        LOG.info(
            "sdr_crm_skip_auto_reply conversation=%s",
            conversation.id,
        )
        return None

    lead = ensure_local_lead_for_conversation(db, conversation)
    if not lead:
        return None

    meta = get_crm_opportunity_meta(lead)
    if meta.get("id") and str(meta.get("status") or "OPEN").upper() != "LOST":
        return meta

    phone = _digits(lead.phone or (conversation.contact.phone if conversation.contact else ""))
    if len(phone) < 10:
        LOG.warning("sdr_crm_skip_short_phone conversation=%s", conversation.id)
        return None

    name = _opportunity_display_name(lead=lead, conversation=conversation)
    if lead.stage in {"NOVO", "TENTATIVA_CONTATO"}:
        lead.stage = "CONTATO_REALIZADO"
    lead.last_contact_at = utcnow()
    if not lead.contact_name:
        lead.contact_name = name

    if not crm_configured():
        meta = set_crm_opportunity_meta(
            lead,
            id=None,
            status="PENDING_CRM",
            source="SDR_FIRST_REPLY",
            client_name=name,
            client_phone=phone,
            created_at=utcnow().isoformat(),
            note="CRM não configurado; lead local aberto aguardando integração.",
        )
        _record_activity(
            db,
            lead=lead,
            agent_user_id=agent_user_id,
            outcome="OPORTUNIDADE_LOCAL",
            description=(
                f"Resposta humana detectada. Lead local aberto com nome={name} "
                f"telefone={phone}. CRM ainda não configurado."
            ),
        )
        return meta

    client = SulnetCRMClient.from_settings()
    payload = {
        "externalId": f"sdr-conversation-{conversation.id}",
        "conversationId": conversation.id,
        "leadId": lead.id,
        "clientName": name,
        "clientPhone": phone,
        "city": lead.city or None,
        "sdrSummary": "Oportunidade aberta pelo SDR IA na primeira resposta humana.",
        "handoffReason": "SDR_FIRST_HUMAN_REPLY",
        "runConsult": False,
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    try:
        result = client.create_opportunity(payload)
    except CRMIntegrationError as exc:
        LOG.warning(
            "sdr_crm_create_failed conversation=%s lead=%s status=%s error=%s",
            conversation.id,
            lead.id,
            exc.status_code,
            exc,
        )
        meta = set_crm_opportunity_meta(
            lead,
            status="CREATE_FAILED",
            source="SDR_FIRST_REPLY",
            client_name=name,
            client_phone=phone,
            created_at=utcnow().isoformat(),
            last_error=str(exc)[:500],
        )
        return meta

    opportunity_id = int(result.get("opportunityId") or 0) or None
    protocol = str(result.get("protocol") or "")
    meta = set_crm_opportunity_meta(
        lead,
        id=opportunity_id,
        protocol=protocol,
        status="OPEN",
        source="SDR_FIRST_REPLY",
        client_name=name,
        client_phone=phone,
        created_at=utcnow().isoformat(),
    )
    _record_activity(
        db,
        lead=lead,
        agent_user_id=agent_user_id,
        outcome="OPORTUNIDADE_CRM",
        description=(
            f"Oportunidade CRM #{opportunity_id or '-'} (protocolo {protocol or '-'}) "
            f"aberta pelo SDR com nome={name} telefone={phone}."
        ),
    )
    LOG.info(
        "sdr_crm_opportunity_created conversation=%s lead=%s opportunity=%s",
        conversation.id,
        lead.id,
        opportunity_id,
    )
    return meta


def mark_sdr_opportunity_lost(
    db: Session,
    *,
    conversation: Conversation,
    decision: AIDecision | None,
    latest_customer_message: str = "",
    agent_user_id: int | None = None,
) -> dict[str, Any] | None:
    """Marca a oportunidade como perdida no CRM e no lead local."""
    lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
    if not lead:
        lead = ensure_local_lead_for_conversation(db, conversation)
    if not lead:
        return None

    code, label = resolve_loss_reason(
        decision,
        latest_customer_message=latest_customer_message,
    )
    meta = get_crm_opportunity_meta(lead)
    opportunity_id = int(meta.get("id") or 0) or None

    # Local: perda comercial + DNC quando pediu para parar.
    lead.stage = "PERDIDO"
    lead.next_action_at = None
    if code in {"OPT_OUT", "NUMERO_ERRADO", "SEM_INTERESSE", "JA_E_CLIENTE"}:
        if decision and (
            decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT"
        ):
            lead.do_not_contact = True
    lead.notes = (
        f"{(lead.notes or '').rstrip()}\n"
        f"[IA SDR] Oportunidade perdida: {label} ({code})."
    ).strip()[-6000:]

    if opportunity_id and crm_configured():
        try:
            client = SulnetCRMClient.from_settings()
            client.mark_lost(
                opportunity_id,
                reason_code=code,
                reason_label=label,
                notes=(
                    f"Encerrado pelo SDR IA. Última mensagem do cliente: "
                    f"{(latest_customer_message or '')[:300]}"
                ),
            )
        except CRMIntegrationError as exc:
            LOG.warning(
                "sdr_crm_mark_lost_failed opportunity=%s status=%s error=%s",
                opportunity_id,
                exc.status_code,
                exc,
            )
            meta = set_crm_opportunity_meta(
                lead,
                status="LOST_SYNC_FAILED",
                loss_reason_code=code,
                loss_reason_label=label,
                lost_at=utcnow().isoformat(),
                last_error=str(exc)[:500],
            )
            return meta

    meta = set_crm_opportunity_meta(
        lead,
        status="LOST",
        loss_reason_code=code,
        loss_reason_label=label,
        lost_at=utcnow().isoformat(),
    )
    _record_activity(
        db,
        lead=lead,
        agent_user_id=agent_user_id,
        outcome="PERDA_CRM",
        description=(
            f"Oportunidade CRM #{opportunity_id or '-'} marcada como perdida. "
            f"Motivo: {label} ({code})."
        ),
    )
    LOG.info(
        "sdr_crm_opportunity_lost conversation=%s lead=%s opportunity=%s reason=%s",
        conversation.id,
        lead.id,
        opportunity_id,
        code,
    )
    return meta


def should_mark_opportunity_lost(decision: AIDecision) -> bool:
    return decision.intent == "DO_NOT_CONTACT" or decision.sales_state == "OPT_OUT"


def sync_sdr_crm_lifecycle(
    db: Session,
    *,
    conversation: Conversation,
    decision: AIDecision,
    latest_message: Message | None,
    agent_user_id: int | None = None,
) -> None:
    """Aplica a regra completa: abre na resposta humana; perde no opt-out."""
    latest_body = latest_message.body if latest_message else ""
    if should_mark_opportunity_lost(decision):
        # Garante que exista a oportunidade antes de marcar perda (mesmo turno).
        if not is_automatic_reply(latest_body):
            ensure_sdr_crm_opportunity(
                db,
                conversation=conversation,
                latest_message=latest_message,
                agent_user_id=agent_user_id,
            )
        mark_sdr_opportunity_lost(
            db,
            conversation=conversation,
            decision=decision,
            latest_customer_message=latest_body,
            agent_user_id=agent_user_id,
        )
        return

    ensure_sdr_crm_opportunity(
        db,
        conversation=conversation,
        latest_message=latest_message,
        agent_user_id=agent_user_id,
    )
