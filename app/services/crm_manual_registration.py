"""Registro manual de conversas no CRM Comercial, sem executar IA ou enviar mensagens."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditLog, Conversation, User
from app.services.audit import write_audit
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient

LOG = logging.getLogger("sulnet_crm_manual")
ACTION_REGISTERED = "CRM_MANUAL_REGISTERED"
ACTION_UNCERTAIN = "CRM_MANUAL_UNCERTAIN"
ACTION_REVIEW = "CRM_MANUAL_REVIEW"
ORIGIN = "Sulnet Prospecta"


class CRMManualError(RuntimeError):
    """Falha explicável ao operador, sem expor tokens ou dados de outros clientes."""


@dataclass(frozen=True)
class CRMSeller:
    username: str
    user_id: int
    funnel_id: int
    funnel_name: str
    stage_id: int


# Mapeamento aprovado a partir dos IDs reais do CRM, não da região ou da fila.
_SELLERS: dict[str, CRMSeller] = {
    "maria gabriela paim peno": CRMSeller("maria.peno", 24, 16, "Celeiro", 55),
    "claudia linde": CRMSeller("claudia.linde", 37, 16, "Celeiro", 55),
    "natine dauana dalla lana hirt": CRMSeller("natine.hirt", 25, 17, "Santa Rosa", 63),
    "fabiano braga": CRMSeller("fabiano.braga", 36, 17, "Santa Rosa", 63),
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(ascii_text.casefold().split())


def seller_for_user(user: User | None) -> CRMSeller | None:
    if user is None or not user.active:
        return None
    return _SELLERS.get(_fold(user.name))


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_key(value: str | None) -> str:
    digits = _digits(value)
    return digits[2:] if digits.startswith("55") and len(digits) in (12, 13) else digits


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _records(payload: Any) -> list[dict[str, Any]]:
    """Aceita formatos comuns de paginação; resposta desconhecida bloqueia a escrita."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "opportunities", "leads", "results", "rows", "data"):
            if key in payload:
                records = _records(payload[key])
                total = _as_int(payload.get("total") or payload.get("totalCount") or payload.get("count"))
                if (total is not None and total > len(records)) or payload.get("nextPage") or payload.get("next"):
                    raise CRMManualError(
                        "A pesquisa do CRM retornou resultados incompletos. Confira o CRM antes de registrar."
                    )
                return records
    raise CRMManualError(
        "O CRM não confirmou o resultado da pesquisa de duplicidades. Registro não realizado."
    )


def _record_phone(record: dict[str, Any]) -> str:
    client = _as_dict(record.get("client") or record.get("customer"))
    return str(
        record.get("clientPhone")
        or record.get("client_phone")
        or record.get("phone")
        or client.get("phone")
        or client.get("clientPhone")
        or ""
    )


def _record_owner(record: dict[str, Any]) -> int | None:
    owner = _as_dict(record.get("assignedUser") or record.get("seller"))
    return _as_int(
        record.get("assignedUserId")
        or record.get("assigned_user_id")
        or record.get("sellerId")
        or record.get("seller_id")
        or owner.get("id")
    )


def _record_id(record: dict[str, Any]) -> int | None:
    return _as_int(record.get("id") or record.get("opportunityId") or record.get("leadId"))


def _closed(record: dict[str, Any]) -> bool:
    status = _fold(str(record.get("status") or record.get("stageName") or ""))
    return status in {
        "ganho", "perdido", "cancelado", "encerrado", "concluido",
        "won", "lost", "closed", "cancelled", "canceled",
    }


def _audit_data(row: AuditLog) -> dict[str, Any]:
    try:
        result = json.loads(row.payload_json or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def registration_status(db: Session, contact_id: int) -> dict[str, Any] | None:
    """Estado conhecido do contato, inclusive de outra conversa com o mesmo número."""
    row = db.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "contact",
            AuditLog.entity_id == str(contact_id),
            AuditLog.action.in_((ACTION_REGISTERED, ACTION_UNCERTAIN, ACTION_REVIEW)),
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {"state": row.action, **_audit_data(row)}


def _log_result(
    db: Session, conversation: Conversation, actor: User, action: str, payload: dict[str, Any]
) -> None:
    write_audit(
        db,
        user=actor,
        action=action,
        entity_type="contact",
        entity_id=conversation.contact_id,
        payload={"conversationId": conversation.id, **payload},
    )
    db.commit()


def _guard_contact_lock(db: Session, contact_id: int) -> None:
    """Serializa cliques concorrentes entre as réplicas Railway do mesmo contato."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 700_000_000_000 + int(contact_id)},
        )


def register_conversation(
    db: Session, *, conversation: Conversation, actor: User, client_name: str
) -> dict[str, Any]:
    seller = seller_for_user(conversation.assigned_user)
    if seller is None:
        raise CRMManualError(
            "Conversa sem vendedor ativo mapeado no CRM. Atribua a um dos vendedores cadastrados."
        )
    if actor.role == "CONSULTOR" and conversation.assigned_user_id != actor.id:
        raise CRMManualError("Somente o responsável pode registrar esta conversa no CRM.")

    name = " ".join((client_name or "").strip().split())
    if len(name) < 2 or len(name) > 150:
        raise CRMManualError("Informe o nome do cliente (de 2 a 150 caracteres).")
    phone = _digits(conversation.contact.phone)
    if len(_phone_key(phone)) not in (10, 11):
        raise CRMManualError("Telefone inválido para registrar no CRM.")

    _guard_contact_lock(db, conversation.contact_id)
    previous = registration_status(db, conversation.contact_id)
    if previous is not None:
        if previous["state"] == ACTION_REGISTERED:
            return {"already": True, **previous}
        raise CRMManualError(
            "Registro anterior precisa de conferência no CRM. Não é seguro enviar novamente."
        )

    crm = SulnetCRMClient.from_settings()
    if not crm.bearer_token:
        raise CRMManualError(
            "Consulta de duplicidades do CRM não configurada. Registro bloqueado por segurança."
        )

    # O CRM não possui unicidade nativa. Não registrar se a busca falhar ou vier
    # em formato desconhecido: isso poderia duplicar oportunidades existentes.
    try:
        found = crm.find_opportunities_by_phone(phone)
        records = _records(found)
        # Resultados sem telefone não permitem decidir se há duplicidade.
        if any(not _record_phone(item) for item in records):
            raise CRMManualError(
                "A consulta do CRM não trouxe todos os telefones. Registro bloqueado para evitar duplicidade."
            )
        matches = [
            item for item in records
            if _phone_key(_record_phone(item)) == _phone_key(phone) and not _closed(item)
        ]
    except CRMIntegrationError as exc:
        LOG.warning("crm_manual_lookup_failed status=%s", exc.status_code)
        raise CRMManualError(
            "Não foi possível verificar oportunidades existentes no CRM. Nenhum registro foi enviado."
        ) from exc

    for item in matches:
        opportunity_id = _record_id(item)
        owner_id = _record_owner(item)
        funnel_id = _as_int(item.get("funnelId") or item.get("funnel_id"))
        if opportunity_id is None or owner_id is None:
            raise CRMManualError(
                "Já existe um resultado no CRM que precisa de conferência manual. Registro bloqueado."
            )
        if owner_id == seller.user_id:
            result = {
                "opportunityId": opportunity_id,
                "protocol": str(item.get("protocol") or ""),
                "sellerUsername": seller.username,
                "assignedUserId": seller.user_id,
                "funnelId": seller.funnel_id,
                "stageId": seller.stage_id,
                "outcome": "existing",
            }
            _log_result(db, conversation, actor, ACTION_REGISTERED, result)
            return {"already": True, **result}
        # O SDR IA também é outro proprietário: jamais criar uma segunda
        # oportunidade sem conferência ou transferência no CRM.
        raise CRMManualError(
            "Já existe oportunidade aberta deste telefone com outro responsável. "
            "Confira ou transfira essa oportunidade no CRM antes de registrar."
        )

    try:
        result = crm.create_seller_lead(
            seller_username=seller.username,
            client_name=name,
            client_phone=phone,
            origin=ORIGIN,
        )
    except CRMIntegrationError as exc:
        # Timeout/5xx é ambíguo: o CRM pode ter gravado mesmo sem responder.
        if exc.status_code is None or exc.status_code >= 500:
            _log_result(
                db, conversation, actor, ACTION_UNCERTAIN,
                {"sellerUsername": seller.username, "statusCode": exc.status_code},
            )
            raise CRMManualError(
                "Não houve confirmação do CRM. Confira o cadastro antes de tentar novamente."
            ) from exc
        raise CRMManualError(
            "O CRM recusou o registro. Confira a integração e os dados do cliente."
        ) from exc

    opportunity_id = _record_id(result)
    assigned_id = _as_int(result.get("assignedUserId"))
    funnel_id = _as_int(result.get("funnelId"))
    stage_id = _as_int(result.get("stageId"))
    if (
        opportunity_id is None
        or assigned_id != seller.user_id
        or funnel_id != seller.funnel_id
        or stage_id != seller.stage_id
    ):
        _log_result(
            db, conversation, actor, ACTION_REVIEW,
            {
                "opportunityId": opportunity_id,
                "sellerUsername": seller.username,
                "assignedUserId": assigned_id,
                "funnelId": funnel_id,
                "stageId": stage_id,
            },
        )
        raise CRMManualError(
            "O CRM respondeu, mas não confirmou vendedor, funil e etapa corretos. "
            "Registro retido para conferência, sem repetir o envio."
        )

    saved = {
        "opportunityId": opportunity_id,
        "protocol": str(result.get("protocol") or ""),
        "sellerUsername": seller.username,
        "assignedUserId": assigned_id,
        "funnelId": funnel_id,
        "stageId": stage_id,
        "outcome": "created",
    }
    _log_result(db, conversation, actor, ACTION_REGISTERED, saved)
    LOG.info("crm_manual_registered opportunity=%s seller=%s", opportunity_id, seller.username)
    return {"already": False, **saved}
