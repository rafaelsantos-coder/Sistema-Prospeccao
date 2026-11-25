"""Entrada web enxuta do Prospecta Conversas.

A pagina nao consulta dados por telefone nem cria um segundo historico.
Todas as conversas e mensagens vem das APIs existentes, que aplicam
escopo por usuario, fila e instancia exclusiva em cada requisicao.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.routers.conversations import _manual_outbound_queues, _manual_queue_channel
from app.security import get_current_user
from app.services.access_control import has_permission
from app.services.campaign_queue import list_quick_chat_templates
from app.web import render


router = APIRouter()


@router.get("/conversas")
@router.get("/conversas/{conversation_id}")
def conversas_mvp(
    request: Request,
    conversation_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    can_reply = has_permission(user, "conversations.reply")
    return render(
        request,
        "conversas_mvp.html",
        user=user,
        page_title="Prospecta Conversas",
        selected_conversation_id=conversation_id,
        can_reply=can_reply,
        can_transfer=has_permission(user, "conversations.transfer"),
        user_id=user.id,
        new_conversation_queues=[
            {"id": queue.id, "name": queue.name, "channel": _manual_queue_channel(db, queue.id)}
            for queue, _instance in _manual_outbound_queues(db, user)
        ] if can_reply else [],
        new_conversation_templates=list_quick_chat_templates(db, user) if can_reply else [],
    )
