"""Webhook da ZapSign: assinatura do contrato chega direto ao Prospecta.

O CRM gera o contrato na ZapSign, mas a API dele (/status) não informa se o
contrato foi assinado. Sem esse aviso o Vendedor IA continuava mandando
"seu contrato continua aguardando assinatura" depois de o cliente assinar
(conversa 472, 04/11). Com este webhook cadastrado na ZapSign, a assinatura
marca o fechamento como assinado e o ciclo do Vendedor envia a mensagem final.

URL: /webhooks/zapsign/<ZAPSIGN_WEBHOOK_SECRET>
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AIClosingState
from app.services.audit import write_audit

LOG = logging.getLogger("sulnet_zapsign")
router = APIRouter()

_TOKEN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_SIGNED = {"signed", "assinado", "doc_signed"}


def _tokens(payload: Any, depth: int = 0) -> set[str]:
    found: set[str] = set()
    if depth > 4:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                found |= _tokens(value, depth + 1)
            elif isinstance(value, str) and (
                "token" in str(key).lower() or "url" in str(key).lower()
            ):
                found |= {item.lower() for item in _TOKEN.findall(value)}
    elif isinstance(payload, list):
        for item in payload[:50]:
            found |= _tokens(item, depth + 1)
    return found


def zapsign_document_signed(payload: dict[str, Any]) -> bool:
    event = str(payload.get("event_type") or payload.get("event") or "").strip().lower()
    doc_status = str(payload.get("status") or "").strip().lower()
    if event == "doc_signed" or doc_status in _SIGNED:
        return True
    signers = payload.get("signers")
    if isinstance(signers, list) and signers:
        return all(
            str((item or {}).get("status") or "").strip().lower() in _SIGNED
            for item in signers
            if isinstance(item, dict)
        )
    return False


def mark_contracts_signed(db: Session, payload: dict[str, Any]) -> list[int]:
    tokens = _tokens(payload)
    if not tokens or not zapsign_document_signed(payload):
        return []
    matched: list[int] = []
    states = db.scalars(
        select(AIClosingState).where(
            AIClosingState.step == "WAITING_SIGNATURE",
            AIClosingState.sign_url != "",
        )
    )
    for state in states:
        url_tokens = {item.lower() for item in _TOKEN.findall(state.sign_url or "")}
        if url_tokens & tokens:
            state.contract_status = "assinado"
            matched.append(state.id)
    return matched


@router.post("/webhooks/zapsign/{secret}")
async def zapsign_webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    expected = settings.zapsign_webhook_secret
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    matched = mark_contracts_signed(db, payload)
    if matched:
        write_audit(
            db,
            user=None,
            action="ZAPSIGN_CONTRACT_SIGNED",
            entity_type="ai_closing_state",
            entity_id=matched[0],
            payload={"closing_state_ids": matched},
        )
    db.commit()
    LOG.info("zapsign_webhook matched=%s", len(matched))
    return {"ok": True, "matched": len(matched)}
