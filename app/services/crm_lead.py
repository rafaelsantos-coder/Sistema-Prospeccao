"""Lead do CRM aberta assim que o cliente escreve.

Regra (06/11/2026): toda conversa iniciada pelo cliente vira lead no CRM. O
mínimo para criar é nome e telefone. Enquanto o cliente não informar o nome
real, usamos o nome do perfil do WhatsApp.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models import Conversation
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient

LOG = logging.getLogger("sulnet_crm_lead")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def ensure_crm_lead(db: Session, conversation: Conversation) -> int | None:
    """Abre a lead no CRM uma única vez por conversa. Nunca lança erro."""
    if conversation.crm_opportunity_id:
        return conversation.crm_opportunity_id
    contact = conversation.contact
    name = (contact.name or contact.business_name or "").strip() if contact else ""
    phone = _digits(contact.phone if contact else "")
    if not name or len(phone) < 10:
        LOG.info("crm_lead_skipped conversation=%s reason=sem_nome_ou_telefone", conversation.id)
        return None
    try:
        client = SulnetCRMClient.from_settings()
        result = client.create_opportunity(
            {
                "clientName": name,
                "clientPhone": phone,
                "conversationId": conversation.id,
                "leadId": conversation.lead_id,
                "runConsult": False,
            }
        )
    except CRMIntegrationError as exc:
        # Tenta de novo no próximo job da conversa.
        LOG.warning("crm_lead_failed conversation=%s status=%s", conversation.id, exc.status_code)
        return None
    opportunity_id = int(result.get("opportunityId") or 0) or None
    if opportunity_id:
        conversation.crm_opportunity_id = opportunity_id
        db.flush()
        LOG.info("crm_lead_opened conversation=%s opportunity=%s", conversation.id, opportunity_id)
    return opportunity_id
