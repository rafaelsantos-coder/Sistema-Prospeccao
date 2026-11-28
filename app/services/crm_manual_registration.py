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

# Login do e-mail Prospecta igual ao username CRM (ex.: jenifer.dutra@...).
_SELLERS_BY_CRM_USERNAME = {
    seller.username.casefold(): seller for seller in {*_SELLERS.values()}
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
    by_crm_username = _SELLERS_BY_CRM_USERNAME.get(login)
    # Se um login dedicado estiver atrelado a outro vendedor conhecido, não
    # permitir registrar oportunidades em um proprietário ambíguo.
    candidates = [item for item in (by_login, by_name, by_crm_username) if item is not None]
    if not candidates:
        return None
    first = candidates[0]
    if any(item != first for item in candidates[1:]):
        return None
    return first


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


_CLOSED_TOKENS = {
    "ganho", "perdido", "cancelado", "encerrado", "concluido",
    "won", "lost", "closed", "cancelled", "canceled",
}


def _token_closed(value: str | None) -> bool:
    folded = _fold(str(value or ""))
    if not folded:
        return False
    if folded in _CLOSED_TOKENS:
        return True
    # Etapas do CRM: "GANHO", "PÓS VENDAS" não bloqueiam novo ciclo comercial.
    return folded.startswith("ganho") or folded.startswith("perdid")


def _closed(record: dict[str, Any]) -> bool:
    """Oportunidade encerrada no CRM (status ou etapa), independente do lead Prospecta."""
    stage = (
        record.get("stageName")
        or record.get("stage_name")
        or _as_dict(record.get("stage")).get("name")
    )
    status = record.get("status")
    return _token_closed(str(status or "")) or _token_closed(str(stage or ""))


def opportunity_registration_is_stale(record: dict[str, Any] | None) -> bool:
    """Registro auditado deixa de valer quando a oportunidade CRM já fechou ou sumiu."""
    if not record:
        return True
    return _closed(record)


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


def _fetch_registration_opportunity(
    crm: SulnetCRMClient | None, opportunity_id: int | None,
) -> dict[str, Any] | None:
    if crm is None or opportunity_id is None:
        return None
    try:
        record = crm.get_opportunity(opportunity_id)
        return record if isinstance(record, dict) and record else None
    except CRMIntegrationError as exc:
        # 404 = oportunidade removida → tratar como stale e liberar novo registro.
        if exc.status_code == 404:
            return None
        LOG.warning(
            "crm_manual_status_lookup_failed opportunity=%s status=%s",
            opportunity_id,
            exc.status_code,
        )
        raise


def active_registration_status(
    db: Session,
    contact_id: int,
    seller_username: str,
    *,
    crm: SulnetCRMClient | None = None,
) -> dict[str, Any] | None:
    """Igual a registration_status, mas ignora vínculos CRM já ganhos/perdidos.

    Isso evita que um cliente antigo em etapa Ganho trave o botão Registrar CRM
    para um novo atendimento legítimo do mesmo telefone/vendedor.
    """
    previous = registration_status(db, contact_id, seller_username)
    if previous is None:
        return None

    state = str(previous.get("state") or "")
    opportunity_id = _as_int(previous.get("opportunityId"))

    if state in {ACTION_UNCERTAIN, ACTION_REVIEW}:
        # Sem opportunityId confirmado: exige conferência humana.
        if opportunity_id is None:
            return previous
        try:
            record = _fetch_registration_opportunity(crm, opportunity_id)
        except CRMIntegrationError:
            return previous
        if opportunity_registration_is_stale(record):
            LOG.info(
                "crm_manual_stale_uncertain contact=%s seller=%s opportunity=%s",
                contact_id,
                seller_username,
                opportunity_id,
            )
            return None
        return previous

    if state != ACTION_REGISTERED:
        return previous

    # REGISTERED sem id: manter bloqueio (estado inconsistente → conferência).
    if opportunity_id is None:
        return previous

    try:
        # Preferir cliente informado; senão criar um só para leitura.
        client = crm or SulnetCRMClient.from_settings()
        record = _fetch_registration_opportunity(client, opportunity_id)
    except CRMIntegrationError:
        # Sem confirmação do CRM, não libera automaticamente (segurança).
        return previous

    if opportunity_registration_is_stale(record):
        LOG.info(
            "crm_manual_stale_registered contact=%s seller=%s opportunity=%s status=%s stage=%s",
            contact_id,
            seller_username,
            opportunity_id,
            (record or {}).get("status"),
            (record or {}).get("stageName") or (record or {}).get("stage_name"),
        )
        return None
    return previous


def register_block_reason(
    *,
    seller: CRMSeller | None,
    registration: dict[str, Any] | None,
    assigned: bool,
) -> str | None:
    """Mensagem clara para a UI quando o formulário não pode abrir."""
    if not assigned:
        return "Atribua a conversa a um vendedor cadastrado no CRM para liberar o registro."
    if seller is None:
        return (
            "O responsável desta conversa não está mapeado no CRM Comercial. "
            "Confira o cadastro do vendedor (nome/login) com a supervisão."
        )
    if registration is None:
        return None
    state = str(registration.get("state") or "")
    protocol = str(registration.get("protocol") or "").strip()
    opp = registration.get("opportunityId")
    if state == ACTION_REGISTERED:
        suffix = f" Protocolo {protocol}." if protocol else (f" Oportunidade #{opp}." if opp else "")
        return f"Já existe um registro ativo no CRM para este contato e vendedor.{suffix}"
    if state in {ACTION_UNCERTAIN, ACTION_REVIEW}:
        return (
            "Há um registro anterior pendente de conferência no CRM. "
            "Confira no CRM Comercial antes de tentar novamente."
        )
    return "Registro CRM indisponível no momento. Acione a supervisão com o log técnico."


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

    previous = active_registration_status(
        db, conversation.contact_id, seller.username, crm=crm,
    )
    if previous is not None:
        if previous["state"] == ACTION_REGISTERED:
            return {"already": True, **previous}
        raise CRMManualError(
            "Registro anterior precisa de conferência no CRM. Não é seguro enviar novamente."
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
