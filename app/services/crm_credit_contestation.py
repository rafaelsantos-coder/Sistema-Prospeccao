"""Contestação humana de crédito no CRM Comercial a partir da conversa.

Não cria oportunidade, não muda funil e não dispara Serasa. O CRM é a fonte
de verdade para elegibilidade, motivos, documentação e decisão gerencial.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, Conversation, User
from app.services.audit import write_audit
from app.services.crm_manual_credit import credit_status
from app.services.crm_manual_registration import (
    CRMManualError,
    _as_int,
    _audit_data,
    _guard_contact_lock,
    registration_status,
    seller_for_user,
)
from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient
from app.services.upload_security import UploadValidationError, validate_upload


LOG = logging.getLogger("sulnet_crm_contestation")
CONTEST_SENT = "CRM_CONTEST_SENT"
CONTEST_UNCERTAIN = "CRM_CONTEST_UNCERTAIN"
CONTEST_REVIEW = "CRM_CONTEST_REVIEW"
MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
CREDIT_ROLES = {"CONSULTOR", "SUPERVISOR"}


@dataclass(frozen=True)
class ContestedOpportunity:
    opportunity_id: int
    analysis_id: int
    seller_username: str
    seller_user_id: int
    funnel_id: int


def _state(db: Session, contact_id: int) -> dict[str, Any] | None:
    row = db.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "contact",
            AuditLog.entity_id == str(contact_id),
            AuditLog.action.in_((CONTEST_SENT, CONTEST_UNCERTAIN, CONTEST_REVIEW)),
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return {"state": row.action, **_audit_data(row)} if row else None


def _write_state(
    db: Session,
    *,
    conversation: Conversation,
    actor: User,
    action: str,
    values: dict[str, Any],
) -> None:
    # Nunca salvar CPF, justificativa ou conteúdo do anexo em auditoria local.
    write_audit(
        db,
        user=actor,
        action=action,
        entity_type="contact",
        entity_id=conversation.contact_id,
        payload={"conversationId": conversation.id, **values},
    )
    db.commit()


def _context(db: Session, conversation: Conversation) -> ContestedOpportunity:
    seller = seller_for_user(conversation.assigned_user)
    registered = registration_status(db, conversation.contact_id)
    consulted = credit_status(db, conversation.contact_id)
    if not seller or not registered or not consulted:
        raise CRMManualError(
            "É necessário registrar a oportunidade e consultar o CPF antes da contestação."
        )
    opp_id = _as_int(registered.get("opportunityId"))
    analysis_id = _as_int(consulted.get("analysisId"))
    if (
        registered.get("state") != "CRM_MANUAL_REGISTERED"
        or consulted.get("state") != "CRM_MANUAL_CREDIT"
        or not opp_id
        or not analysis_id
        or _as_int(consulted.get("opportunityId")) != opp_id
        or registered.get("sellerUsername") != seller.username
        or consulted.get("sellerUsername") != seller.username
    ):
        raise CRMManualError(
            "O vínculo entre conversa, vendedor, oportunidade e análise precisa ser conferido no CRM."
        )
    return ContestedOpportunity(
        opportunity_id=opp_id,
        analysis_id=analysis_id,
        seller_username=seller.username,
        seller_user_id=seller.user_id,
        funnel_id=seller.funnel_id,
    )


def _request_scope(ctx: ContestedOpportunity) -> dict[str, Any]:
    return {
        "opportunityId": ctx.opportunity_id,
        "analysisId": ctx.analysis_id,
        "sellerUsername": ctx.seller_username,
    }


def _verify_response(ctx: ContestedOpportunity, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CRMManualError("CRM retornou resposta inesperada. Nenhuma operação deve ser repetida.")
    if (
        _as_int(payload.get("opportunityId")) != ctx.opportunity_id
        or _as_int(payload.get("analysisId")) != ctx.analysis_id
        or _as_int(payload.get("assignedUserId")) != ctx.seller_user_id
        or _as_int(payload.get("funnelId")) != ctx.funnel_id
        or str(payload.get("sellerUsername") or "") != ctx.seller_username
    ):
        raise CRMManualError(
            "O CRM não confirmou a mesma análise, vendedor e funil. A operação requer conferência."
        )
    return payload


def _reason_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("reasons")
    if not isinstance(rows, list):
        raise CRMManualError("Os motivos de contestação não foram retornados corretamente pelo CRM.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reason in rows:
        if not isinstance(reason, dict) or reason.get("active") is False:
            continue
        key = str(reason.get("key") or "").strip()
        label = str(reason.get("label") or "").strip()
        min_files = _as_int(reason.get("minAttachments"))
        max_files = _as_int(reason.get("maxAttachments"))
        doc_type = str(reason.get("attachmentDocType") or "").strip()
        if (
            not re.fullmatch(r"[a-z0-9_]{1,64}", key)
            or not label or len(label) > 120 or key in seen
            or not isinstance(reason.get("requiresJustification"), bool)
            or not isinstance(reason.get("requiresDocConfirmation"), bool)
            or min_files is None or max_files is None
            or min_files < 0 or max_files < min_files or max_files > 3
            or (max_files > 0 and doc_type not in {"contestacao_fatura", "contestacao_anexo"})
            or (max_files == 0 and doc_type)
        ):
            raise CRMManualError(
                "O catálogo de motivos do CRM está incompleto. Solicite a validação da integração."
            )
        seen.add(key)
        result.append({
            "key": key,
            "label": label,
            "requiresJustification": reason["requiresJustification"],
            "requiresDocConfirmation": reason["requiresDocConfirmation"],
            "minAttachments": min_files,
            "maxAttachments": max_files,
            "attachmentDocType": doc_type,
        })
    if not result:
        raise CRMManualError("Nenhum motivo de contestação ativo disponível no CRM.")
    return result


def _safe_status(ctx: ContestedOpportunity, payload: dict[str, Any]) -> dict[str, Any]:
    _verify_response(ctx, payload)
    # Só o CRM decide canContest: nunca inferir de uma reprovação local.
    raw_status = str(payload.get("status") or "")[:100]
    allowed = payload.get("canContest") is True and raw_status not in {
        "Contestação enviada", "Em análise gerencial",
    }
    return {
        "opportunityId": ctx.opportunity_id,
        "analysisId": ctx.analysis_id,
        "status": raw_status,
        "result": str(payload.get("result") or "")[:100],
        "canContest": allowed,
        "managementDecision": str(payload.get("managementDecision") or "")[:120],
        "contestationAt": str(payload.get("contestationAt") or "")[:80],
    }


def get_contestation_panel(db: Session, *, conversation: Conversation, actor: User) -> dict[str, Any]:
    """Leitura S2S: não invoca /consult e não faz consulta Serasa."""
    if not settings.sulnet_crm_contestation_enabled:
        raise CRMManualError("Contestação ainda não habilitada na integração com o CRM.")
    ctx = _context(db, conversation)
    client = SulnetCRMClient.from_settings()
    try:
        status_payload = client.seller_contestation_status(**{
            "opportunity_id": ctx.opportunity_id,
            "analysis_id": ctx.analysis_id,
            "seller_username": ctx.seller_username,
        })
        status = _safe_status(ctx, status_payload)
        state = _state(db, conversation.contact_id)
        if state and state.get("state") in {CONTEST_UNCERTAIN, CONTEST_REVIEW}:
            status["canContest"] = False
            status["reviewRequired"] = True
        else:
            status["reviewRequired"] = False
        # A UI do CRM só permite que vendedor e supervisão enviem contestação.
        if actor.role not in CREDIT_ROLES:
            status["canContest"] = False
            status["readOnly"] = True
        elif actor.id != conversation.assigned_user_id:
            status["canContest"] = False
            status["readOnly"] = True
        else:
            status["readOnly"] = False
        status["reasons"] = []
        if status["canContest"]:
            reason_payload = client.seller_contestation_reasons(**{
                "opportunity_id": ctx.opportunity_id,
                "analysis_id": ctx.analysis_id,
                "seller_username": ctx.seller_username,
            })
            _verify_response(ctx, reason_payload)
            status["reasons"] = _reason_list(reason_payload)
        return status
    except CRMIntegrationError as exc:
        LOG.warning("crm_contestation_read_failed status=%s code=%s", exc.status_code, exc.error_code)
        raise CRMManualError(
            "Não foi possível consultar a elegibilidade e os motivos no CRM. Tente atualizar mais tarde."
        ) from exc


def send_contestation(
    db: Session,
    *,
    conversation: Conversation,
    actor: User,
    reason_type: str,
    justification: str,
    doc_confirmed: bool,
    documents: list[tuple[str, str, bytes]],
) -> dict[str, Any]:
    if not settings.sulnet_crm_contestation_enabled:
        raise CRMManualError("Contestação ainda não habilitada na integração com o CRM.")
    if actor.role not in CREDIT_ROLES or actor.id != conversation.assigned_user_id:
        raise CRMManualError("A contestação deve ser enviada pelo vendedor responsável ou sua supervisão autorizada.")

    ctx = _context(db, conversation)
    _guard_contact_lock(db, conversation.contact_id)
    past = _state(db, conversation.contact_id)
    if past and past.get("state") in {CONTEST_UNCERTAIN, CONTEST_REVIEW}:
        raise CRMManualError(
            "Existe uma contestação anterior sem confirmação. Confira no CRM antes de repetir."
        )
    # Consulta de elegibilidade, somente leitura: nunca POST /consult.
    panel = get_contestation_panel(db, conversation=conversation, actor=actor)
    if not panel["canContest"]:
        raise CRMManualError("O CRM não permite contestação neste momento.")
    chosen = next((r for r in panel["reasons"] if r["key"] == reason_type), None)
    if not chosen:
        raise CRMManualError("Selecione um motivo válido retornado pelo CRM.")

    justification = " ".join((justification or "").split())
    if len(justification) > 4000:
        raise CRMManualError("A justificativa excede o limite de 4.000 caracteres.")
    if chosen["requiresJustification"] and not justification:
        raise CRMManualError("A justificativa é obrigatória para este motivo.")
    if chosen["requiresDocConfirmation"] and not doc_confirmed:
        raise CRMManualError("É necessário confirmar o documento exigido pelo CRM.")
    if not chosen["minAttachments"] <= len(documents) <= chosen["maxAttachments"]:
        raise CRMManualError("A quantidade de documentos não corresponde à exigida pelo motivo.")

    for filename, mimetype, blob in documents:
        if len(blob) > MAX_FILE_SIZE:
            raise CRMManualError("Cada documento pode ter no máximo 10 MB.")
        try:
            extension = validate_upload(filename=filename, mime_type=mimetype, data=blob)
        except UploadValidationError as exc:
            raise CRMManualError(str(exc)) from exc
        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise CRMManualError("Anexe somente PDF, JPG ou PNG.")

    try:
        crm = SulnetCRMClient.from_settings()
        attachment_ids: list[int] = []
        for filename, mimetype, blob in documents:
            # Nome aleatório elimina CPF/nome de arquivo original em storage/logs.
            extension = Path(filename).suffix.lower()
            safe_name = f"contestacao-{uuid4().hex}{extension}"
            uploaded = crm.upload_seller_contestation_file(
                opportunity_id=ctx.opportunity_id,
                analysis_id=ctx.analysis_id,
                seller_username=ctx.seller_username,
                document_type=chosen["attachmentDocType"],
                filename=safe_name,
                mime_type=mimetype,
                data=blob,
            )
            _verify_response(ctx, uploaded)
            attachment_id = _as_int(uploaded.get("attachmentId"))
            if not attachment_id:
                raise CRMManualError("O CRM não confirmou o ID do documento enviado.")
            attachment_ids.append(attachment_id)

        result = crm.submit_seller_contestation(
            opportunity_id=ctx.opportunity_id,
            analysis_id=ctx.analysis_id,
            seller_username=ctx.seller_username,
            reason_type=reason_type,
            justification=justification,
            doc_confirmed=doc_confirmed,
            attachment_ids=attachment_ids,
        )
    except CRMIntegrationError as exc:
        LOG.warning("crm_contestation_failed status=%s code=%s", exc.status_code, exc.error_code)
        if exc.status_code is None or exc.status_code >= 500:
            _write_state(
                db, conversation=conversation, actor=actor, action=CONTEST_UNCERTAIN,
                values={
                    "opportunityId": ctx.opportunity_id,
                    "analysisId": ctx.analysis_id,
                    "sellerUsername": ctx.seller_username,
                },
            )
            raise CRMManualError(
                "O CRM não confirmou o envio. Confira o histórico antes de contestar novamente."
            ) from exc
        raise CRMManualError(
            "O CRM recusou a contestação. Confira os dados, a elegibilidade e os documentos."
        ) from exc
    except CRMManualError:
        raise

    try:
        _verify_response(ctx, result)
        if str(result.get("status") or "") != "Contestação enviada":
            raise CRMManualError("O CRM não confirmou que a contestação foi enviada.")
    except CRMManualError:
        _write_state(
            db, conversation=conversation, actor=actor, action=CONTEST_REVIEW,
            values={
                "opportunityId": ctx.opportunity_id,
                "analysisId": ctx.analysis_id,
                "sellerUsername": ctx.seller_username,
            },
        )
        raise

    saved = {
        "opportunityId": ctx.opportunity_id,
        "analysisId": ctx.analysis_id,
        "sellerUsername": ctx.seller_username,
        "status": "Contestação enviada",
        "reasonType": reason_type,
        "attachmentCount": len(documents),
    }
    _write_state(db, conversation=conversation, actor=actor, action=CONTEST_SENT, values=saved)
    LOG.info("crm_contestation_sent opportunity_id=%s analysis_id=%s", ctx.opportunity_id, ctx.analysis_id)
    return saved
