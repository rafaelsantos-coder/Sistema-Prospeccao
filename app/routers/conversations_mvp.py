"""MVP de Conversas: interface leve, sem novo banco ou integrações de CRM.

Reutiliza a sessão, ACL por conversa e rotas de mensagens do Prospecta.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, RedirectResponse

from app.database import get_db
from app.routers.conversations import _get_conversation, send_message
from app.security import get_current_user, validate_csrf
from app.services.access_control import has_permission
from app.services.whatsapp_presence import is_user_whatsapp_online
from app.web import render

router = APIRouter()


@router.get("/conversations/mvp")
def mvp_conversations(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not has_permission(user, "conversations.view"):
        raise HTTPException(status_code=403, detail="Acesso não autorizado.")
    return render(
        request,
        "conversations/mvp.html",
        user=user,
        page_title="MVP de Conversas",
        can_reply=has_permission(user, "conversations.reply"),
    )


@router.post("/conversations/mvp/{conversation_id}/send")
def mvp_send_message(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    """Envia pelo mesmo fluxo do inbox; converte o redirecionamento em JSON.

    O endpoint não contorna permissão, bloqueios, fila, instância nem o
    provedor. Erros da rota legada são retornados ao cliente sem expor flash
    de outra interação. Nenhuma mensagem é reenviada automaticamente.
    """
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Entre novamente no Prospecta.")
    if not has_permission(user, "conversations.reply"):
        raise HTTPException(status_code=403, detail="Sem permissão para responder.")
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        raise HTTPException(status_code=403, detail="Entre no WhatsApp para responder.")
    validate_csrf(request, csrf_token)
    if not message.strip() or len(message) > 4096:
        raise HTTPException(status_code=400, detail="Digite uma mensagem de até 4096 caracteres.")
    if _get_conversation(db, user, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Atendimento não está mais disponível para você.")

    previous = list(request.session.get("flashes", []))
    response = send_message(
        conversation_id=conversation_id,
        request=request,
        csrf_token=csrf_token,
        message=message,
        db=db,
    )
    current = list(request.session.get("flashes", []))
    new_flashes = current[len(previous):]
    if new_flashes:
        request.session["flashes"] = previous
        relevant = next(
            (f for f in new_flashes if f.get("category") in {"error", "warning"}),
            None,
        )
        if relevant:
            return JSONResponse(
                {"ok": False, "error": str(relevant.get("message") or "Envio não realizado.")},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
    location = response.headers.get("location", "")
    if not location.startswith(f"/conversations/{conversation_id}"):
        return JSONResponse(
            {"ok": False, "error": "Atendimento indisponível. Atualize a página."},
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"ok": True, "message": "Solicitação de envio processada. Confira a conversa."},
        headers={"Cache-Control": "no-store"},
    )
