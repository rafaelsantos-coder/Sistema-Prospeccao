"""Consulta humana de CPF na oportunidade já registrada no CRM Comercial.

Não armazena CPF no banco do Prospecta nem usa as credenciais personificadas
do SDR IA. Depende de webhook comercial autenticado e expressamente habilitado.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Conversation, User
from app.services.audit import write_audit
from app.services.crm_manual_registration import (
    ACTION_REGISTERED,
    CRMManualError,
    _as_int,
    _contact_crm_status,
    _guard_contact_lock,
    registration_status,
    seller_accepts_funnel,
    seller_for_user,
)
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient

LOG = logging.getLogger("sulnet_crm_manual_credit")

ACTION_CREDIT = "CRM_MANUAL_CREDIT"
ACTION_UNCERTAIN = "CRM_CREDIT_UNCERTAIN"
ACTION_REVIEW = "CRM_CREDIT_REVIEW"


def valid_cpf(cpf: str) -> bool:
    """Valida CPF brasileiro com dígitos verificadores, sem gravá-lo."""
    if not isinstance(cpf, str):
        return False
    digits = re.sub(r"\D", "", cpf)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for length in (9, 10):
        check = (sum(int(digits[i]) * (length + 1 - i) for i in range(length)) * 10) % 11
        if check == 10:
            check = 0
        if int(digits[length]) != check:
            return False
    return True


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).casefold().strip()


def credit_status(
    db: Session, contact_id: int, seller_username: str, opportunity_id: int,
) -> dict[str, Any] | None:
    return _contact_crm_status(
        db, contact_id, seller_username, (ACTION_CREDIT, ACTION_UNCERTAIN, ACTION_REVIEW),
        opportunity_id=opportunity_id,
    )


def _audit(
    db: Session,
    *,
    actor: User,
    conversation: Conversation,
    action: str,
    result: dict[str, Any],
) -> None:
    # Os dados de auditoria têm SOMENTE os IDs e o status: jamais o CPF.
    write_audit(
        db,
        user=actor,
        action=action,
        entity_type="contact",
        entity_id=conversation.contact_id,
        payload={"conversationId": conversation.id, **result},
    )
    db.commit()


def _decision(result: dict[str, Any]) -> tuple[str, str]:
    # A API contratada retorna "result" canônico, diferente do status Serasa bruto.
    status = str(result.get("result") or result.get("status") or "").strip()
    normalized = _fold(status)
    if normalized == "aprovado com taxa":
        return "APROVADO_COM_TAXA", "Aprovado com taxa"
    if normalized == "reprovado":
        return "REPROVADO", "Reprovado"
    if normalized == "aprovado":
        return "APROVADO", "Aprovado"
    if normalized == "em analise":
        return "EM_ANALISE", "Em análise"
    # Caso novo/desconhecido nunca vira "Aprovado" por inferência local.
    return "EM_ANALISE", "Em análise"


def consult_registered_cpf(
    db: Session,
    *,
    conversation: Conversation,
    actor: User,
    cpf: str,
) -> dict[str, Any]:
    if not settings.sulnet_crm_manual_credit_enabled:
        raise CRMManualError("Consulta de CPF ainda não habilitada na integração com o CRM.")

    seller = seller_for_user(conversation.assigned_user)
    if seller is None:
        raise CRMManualError("Esta conversa não possui vendedor ativo mapeado no CRM.")
    if actor.role == "CONSULTOR" and conversation.assigned_user_id != actor.id:
        raise CRMManualError("Somente o responsável pode solicitar a consulta de CPF.")

    if not valid_cpf(cpf):
        raise CRMManualError("CPF inválido. Confira os 11 dígitos informados.")
    digits = re.sub(r"\D", "", cpf)

    _guard_contact_lock(db, conversation.contact_id)
    registered = registration_status(db, conversation.contact_id, seller.username)
    opp_id = _as_int(registered.get("opportunityId")) if registered else None
    if not registered or registered.get("state") != ACTION_REGISTERED or not opp_id:
        raise CRMManualError(
            "Primeiro registre nome e telefone no CRM com o vendedor responsável."
        )

    registered_funnel = _as_int(registered.get("funnelId")) or seller.funnel_id
    if not seller_accepts_funnel(seller, registered_funnel):
        raise CRMManualError(
            "A oportunidade está em um funil não autorizado para este vendedor."
        )
    previous = credit_status(db, conversation.contact_id, seller.username, opp_id)
    if previous:
        if previous["state"] == ACTION_CREDIT:
            return {"already": True, **previous}
        raise CRMManualError(
            "A consulta anterior precisa ser conferida no CRM antes de novo envio."
        )

    try:
        client = SulnetCRMClient.from_settings()
        result = client.consult_seller_credit(
            opportunity_id=opp_id,
            seller_username=seller.username,
            cpf=digits,
        )
    except CRMIntegrationError as exc:
        LOG.warning("crm_manual_credit_failed status=%s code=%s", exc.status_code, exc.error_code)
        if exc.status_code in {404, 405, 501}:
            raise CRMManualError(
                "A API de consulta de crédito ainda não foi disponibilizada pelo CRM."
            ) from exc
        if exc.status_code is None or exc.status_code >= 500:
            _audit(
                db,
                actor=actor,
                conversation=conversation,
                action=ACTION_UNCERTAIN,
                result={
                    "opportunityId": opp_id,
                    "sellerUsername": seller.username,
                    "statusCode": exc.status_code,
                },
            )
            raise CRMManualError(
                "O CRM não confirmou a consulta. Confira a oportunidade antes de repetir."
            ) from exc
        raise CRMManualError(
            "O CRM não autorizou ou não concluiu a consulta. Verifique a oportunidade e o CPF."
        ) from exc

    # A resposta deve identificar a MESMA oportunidade e o MESMO vendedor.
    ret_opp = _as_int(result.get("opportunityId") or result.get("id"))
    ret_seller = _as_int(result.get("assignedUserId"))
    ret_funnel = _as_int(result.get("funnelId"))
    ret_username = str(result.get("sellerUsername") or "")
    if (ret_opp != opp_id or ret_seller != seller.user_id
            or ret_funnel != registered_funnel or ret_username != seller.username):
        _audit(
            db,
            actor=actor,
            conversation=conversation,
            action=ACTION_REVIEW,
            result={"opportunityId": opp_id, "sellerUsername": seller.username},
        )
        raise CRMManualError(
            "O CRM retornou identificação divergente. Consulte o registro antes de continuar."
        )

    code, label = _decision(result)
    saved = {
        "opportunityId": opp_id,
        "sellerUsername": seller.username,
        "funnelId": ret_funnel,
        "status": code,
        "statusLabel": label,
        "analysisId": _as_int(result.get("analysisId")),
        "protocol": str(result.get("protocol") or registered.get("protocol") or ""),
        "analysisProtocol": str(result.get("analysisProtocol") or ""),
        "outcome": str(result.get("outcome") or ""),
    }
    _audit(db, actor=actor, conversation=conversation, action=ACTION_CREDIT, result=saved)
    LOG.info("crm_manual_credit_registered opportunity=%s decision=%s", opp_id, code)
    return {"already": False, **saved}
