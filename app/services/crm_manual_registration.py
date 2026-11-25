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
from app.services.origin_campaigns import (
    DEFAULT_CRM_ORIGIN,
    resolve_conversation_origin,
)

LOG = logging.getLogger("sulnet_crm_manual")
ACTION_REGISTERED = "CRM_MANUAL_REGISTERED"
ACTION_UNCERTAIN = "CRM_MANUAL_UNCERTAIN"
ACTION_REVIEW = "CRM_MANUAL_REVIEW"
ORIGIN = DEFAULT_CRM_ORIGIN


class CRMManualError(RuntimeError):
    """Falha explicável ao operador, sem expor tokens ou dados de outros clientes."""


@dataclass(frozen=True)
class CRMSeller:
    username: str
    user_id: int
    funnel_id: int
    funnel_name: str
    stage_id: int
    additional_funnel_ids: tuple[int, ...] = ()


# Mapeamento aprovado a partir dos IDs reais do CRM, não da região ou da fila.
_SELLERS: dict[str, CRMSeller] = {
    "maria gabriela paim peno": CRMSeller("maria.peno", 24, 16, "Celeiro", 55),
    "claudia linde": CRMSeller("claudia.linde", 37, 16, "Celeiro", 55),
    "natine dauana dalla lana hirt": CRMSeller("natine.hirt", 25, 17, "Santa Rosa", 63),
    "fabiano braga": CRMSeller("fabiano.braga", 36, 17, "Santa Rosa", 63),
    "gabriele naiane barbosa": CRMSeller("gabriele.barbosa", 42, 17, "Santa Rosa", 63, (23,)),
    "michele zandona": CRMSeller("michele.zandona", 43, 17, "Santa Rosa", 63, (23,)),
    # Vendedoras confirmadas no CRM Comercial (25/11/2026).
    # Os nomes longos são apenas aliases do login de usuário do Prospecta;
    # o sellerUsername enviado à API é sempre o login oficial do CRM.
    "jenifer garcia dutra": CRMSeller("jenifer.dutra", 8, 18, "Digital", 71, (23, 24)),
    "jennifer garcia dutra": CRMSeller("jenifer.dutra", 8, 18, "Digital", 71, (23, 24)),
    "daiane mattos": CRMSeller("daiane.mattos", 11, 18, "Digital", 71, (23, 24)),
    "daiane dos santos mattos": CRMSeller("daiane.mattos", 11, 18, "Digital", 71, (23, 24)),
    "andressa reus": CRMSeller("andressa.reus", 4, 18, "Digital", 71, (23, 24)),
    "larissa almeida": CRMSeller("larissa.almeida", 40, 15, "Missões", 47, (23, 24)),
    "larissa da rosa de almeida": CRMSeller("larissa.almeida", 40, 15, "Missões", 47, (23, 24)),
    # Conta rafael.vendedor no Prospecta -> usuário gerencia rafael.santos no CRM.
    # IDs/funis confirmados pelo CRM em 25/11/2026; sem impersonação do SDR IA.
    "rafael trennepohl": CRMSeller(
        "rafael.santos", 7, 15, "Missões", 47, (16, 17, 18, 11, 3, 13, 20, 22, 23, 24),
    ),
    "rafael.vendedor": CRMSeller(
        "rafael.santos", 7, 15, "Missões", 47, (16, 17, 18, 11, 3, 13, 20, 22, 23, 24),
    ),
}

# A tabela de usuários do Prospecta possui e-mail de login, não username.
# Resolver APENAS o login exato solicitado sem inferir por região ou domínio.
_PROSPECTA_ACCOUNT_SELLERS = {
    "rafael.vendedor": _SELLERS["rafael trennepohl"],
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(ascii_text.casefold().split())


def seller_for_user(user: User | None) -> CRMSeller | None:
    if user is None or not user.active:
        return None
    by_name = _SELLERS.get(_fold(user.name))
    login = (user.email or "").strip().casefold().partition("@")[0]
    by_login = _PROSPECTA_ACCOUNT_SELLERS.get(login)
    # Se um login dedicado estiver atrelado a outro vendedor conhecido, não
    # permitir registrar oportunidades em um proprietário ambíguo.
    if by_login is not None and by_name is not None and by_login != by_name:
        return None
    return by_login or by_name


def seller_accepts_funnel(seller: CRMSeller, funnel_id: int | None) -> bool:
    """Confere funil primário ou adicional autorizado, sem inferir pela fila local."""
    return funnel_id is not None and funnel_id in (seller.funnel_id, *seller.additional_funnel_ids)


def _verified_stage(
    seller: CRMSeller,
    funnel_id: int | None,
    stage_id: int | None,
    *,
    newly_created: bool = True,
) -> bool:
    """Valida a etapa inicial do funil principal sem inventar etapas de funis adicionais."""
    return (
        seller_accepts_funnel(seller, funnel_id)
        and stage_id is not None and stage_id > 0
        and (not newly_created or funnel_id != seller.funnel_id or stage_id == seller.stage_id)
    )


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
                pagination = _as_dict(payload.get("pagination") or payload.get("meta"))
                total = _as_int(
                    payload.get("total") or payload.get("totalCount") or pagination.get("total")
                    or pagination.get("totalCount")
                )
                page = _as_int(payload.get("page") or pagination.get("page")) or 1
                pages = _as_int(payload.get("totalPages") or pagination.get("totalPages"))
                has_more = (
                    payload.get("hasMore") or pagination.get("hasMore")
                    or payload.get("nextPage") or pagination.get("nextPage")
                    or payload.get("next") or pagination.get("next")
                    or (pages is not None and page < pages)
                )
                if (total is not None and total > len(records)) or has_more:
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


def _contact_crm_status(
    db: Session,
    contact_id: int,
    seller_username: str,
    actions: tuple[str, ...],
    *,
    opportunity_id: int | None = None,
    analysis_id: int | None = None,
) -> dict[str, Any] | None:
    rows = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "contact",
            AuditLog.entity_id == str(contact_id),
            AuditLog.action.in_(actions),
        )
        .order_by(AuditLog.id.desc())
    )
    for row in rows:
        data = _audit_data(row)
        if data.get("sellerUsername") != seller_username:
            continue
        if opportunity_id is not None and _as_int(data.get("opportunityId")) != opportunity_id:
            continue
        if analysis_id is not None and _as_int(data.get("analysisId")) != analysis_id:
            continue
        return {"state": row.action, **data}
    return None


def registration_status(db: Session, contact_id: int, seller_username: str) -> dict[str, Any] | None:
    """Estado do registro do vendedor, inclusive em outra conversa do mesmo contato."""
    return _contact_crm_status(
        db, contact_id, seller_username, (ACTION_REGISTERED, ACTION_UNCERTAIN, ACTION_REVIEW),
    )


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
    previous = registration_status(db, conversation.contact_id, seller.username)
    if previous is not None:
        if previous["state"] == ACTION_REGISTERED:
            return {"already": True, **previous}
        raise CRMManualError(
            "Registro anterior precisa de conferência no CRM. Não é seguro enviar novamente."
        )

    try:
        crm = SulnetCRMClient.from_settings()
    except CRMIntegrationError as exc:
        raise CRMManualError(
            "A conexão com o CRM Comercial não está configurada. Registro não realizado."
        ) from exc
    if not crm.bearer_token:
        raise CRMManualError(
            "Consulta de duplicidades do CRM não configurada. Registro bloqueado por segurança."
        )

    # Reutiliza oportunidades abertas do vendedor quando a busca confirma o vínculo.
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
        owner_id = _record_owner(item)
        if owner_id is not None and owner_id != seller.user_id:
            continue
        opportunity_id = _record_id(item)
        if opportunity_id is None or owner_id is None:
            raise CRMManualError(
                "Já existe um resultado no CRM que precisa de conferência manual. Registro bloqueado."
            )
        existing_funnel = _as_int(item.get("funnelId")) or seller.funnel_id
        existing_stage = _as_int(item.get("stageId"))
        if existing_stage is None and existing_funnel == seller.funnel_id:
            existing_stage = seller.stage_id
        if not _verified_stage(seller, existing_funnel, existing_stage, newly_created=False):
            raise CRMManualError(
                "O CRM encontrou oportunidade do vendedor, mas o funil ou a etapa "
                "não foram confirmados. Confira antes de prosseguir."
            )
        result = {
            "opportunityId": opportunity_id,
            "protocol": str(item.get("protocol") or ""),
            "sellerUsername": seller.username,
            "assignedUserId": seller.user_id,
            "funnelId": existing_funnel,
            "stageId": existing_stage,
            "outcome": "existing",
        }
        _log_result(db, conversation, actor, ACTION_REGISTERED, result)
        return {"already": True, **result}

    crm_origin, origin_match = resolve_conversation_origin(db, conversation.id)
    origin_payload = {
        "origin": crm_origin,
        "originCampaignId": origin_match.campaign_id if origin_match else None,
        "originCampaignName": origin_match.campaign_name if origin_match else None,
    }

    try:
        result = crm.create_seller_lead(
            seller_username=seller.username,
            client_name=name,
            client_phone=phone,
            origin=crm_origin,
        )
    except CRMIntegrationError as exc:
        # Timeout/5xx é ambíguo: o CRM pode ter gravado mesmo sem responder.
        if exc.status_code is None or exc.status_code >= 500:
            _log_result(
                db, conversation, actor, ACTION_UNCERTAIN,
                {
                    "sellerUsername": seller.username,
                    "statusCode": exc.status_code,
                    **origin_payload,
                },
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
        or not _verified_stage(seller, funnel_id, stage_id)
    ):
        _log_result(
            db, conversation, actor, ACTION_REVIEW,
            {
                "opportunityId": opportunity_id,
                "sellerUsername": seller.username,
                "assignedUserId": assigned_id,
                "funnelId": funnel_id,
                "stageId": stage_id,
                **origin_payload,
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
        **origin_payload,
    }
    _log_result(db, conversation, actor, ACTION_REGISTERED, saved)
    LOG.info(
        "crm_manual_registered opportunity=%s seller=%s origin=%s campaign=%s",
        opportunity_id,
        seller.username,
        crm_origin,
        origin_match.campaign_id if origin_match else None,
    )
    return {"already": False, **saved}
