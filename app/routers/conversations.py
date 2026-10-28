from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import JSONResponse, RedirectResponse

from app.database import get_db
from app.models import (
    Activity,
    Contact,
    Conversation,
    ConversationAssignment,
    Lead,
    Message,
    User,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.ai_sdr import latest_run_for_conversation
from app.services.audit import write_audit
from app.services.conversation_engine import (
    CLOSED_CONVERSATION_STATUSES,
    OPEN_CONVERSATION_STATUSES,
    apply_conversation_scope,
    conversation_visible_to_user,
    ensure_contact,
    ensure_open_conversation,
    mark_conversation_read,
    register_message,
)
from app.services.media_processing import MediaProcessingError, normalize_audio
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.metrics import apply_user_scope
from app.services.queue_routing import queue_for_conversation, queue_member_user_ids
from app.services.whatsapp_presence import is_user_whatsapp_online, online_user_ids
from app.services.zapi import ZApiError, ZApiService
from app.timeutils import utcnow
from app.web import flash, render


router = APIRouter()


def _user_or_redirect(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        flash(
            request,
            "Entre no WhatsApp para acessar o canal de conversas e receber atendimentos.",
            "warning",
        )
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _conversation_stmt(user: User):
    return apply_conversation_scope(
        select(Conversation).options(
            selectinload(Conversation.contact),
            selectinload(Conversation.lead),
            selectinload(Conversation.assigned_user),
            selectinload(Conversation.branch),
        ),
        user,
    )


def _get_conversation(db: Session, user: User, conversation_id: int) -> Conversation | None:
    return db.scalar(_conversation_stmt(user).where(Conversation.id == conversation_id))


def _eligible_users(db: Session, user: User, conversation: Conversation | None = None) -> list[User]:
    online_ids = online_user_ids(db)
    if not online_ids:
        return []
    stmt = select(User).where(
        User.active.is_(True),
        User.role == "CONSULTOR",
        User.id.in_(online_ids),
    ).order_by(User.name)
    users = list(db.scalars(stmt))

    if conversation:
        service_queue = queue_for_conversation(db, conversation.id)
        if service_queue:
            member_ids = set(queue_member_user_ids(db, service_queue.id))
            users = [item for item in users if item.id in member_ids]
        elif conversation.team:
            users = [item for item in users if item.team == conversation.team]

    if user.role == "SUPERVISOR":
        if user.branch_id:
            users = [item for item in users if item.branch_id == user.branch_id]
        else:
            users = [item for item in users if item.team == user.team]
    elif user.role == "CONSULTOR":
        users = [item for item in users if item.team == user.team]
        if user.branch_id:
            users = [item for item in users if item.branch_id == user.branch_id]

    if conversation and conversation.branch_id:
        same_branch = [item for item in users if item.branch_id == conversation.branch_id]
        if same_branch:
            users = same_branch

    if conversation and conversation.assigned_user_id:
        users = [item for item in users if item.id != conversation.assigned_user_id]
    return users


def _inbox_query(db: Session, user: User, *, q: str, view: str) -> list[Conversation]:
    stmt = _conversation_stmt(user).join(Contact, Conversation.contact_id == Contact.id)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.outerjoin(Lead, Conversation.lead_id == Lead.id).where(
            or_(
                Contact.phone.ilike(term),
                Contact.name.ilike(term),
                Contact.business_name.ilike(term),
                Lead.business_name.ilike(term),
                Lead.contact_name.ilike(term),
            )
        )
    if view == "unread":
        stmt = stmt.where(Conversation.unread_count > 0)
    elif view == "closed":
        stmt = stmt.where(Conversation.status.in_(CLOSED_CONVERSATION_STATUSES))
    elif view == "queue":
        stmt = stmt.where(Conversation.assigned_user_id.is_(None))
    elif view == "mine" and user.role in {"ADMIN", "GESTOR", "SUPERVISOR"}:
        stmt = stmt.where(Conversation.assigned_user_id == user.id)
    else:
        stmt = stmt.where(Conversation.status.in_(OPEN_CONVERSATION_STATUSES))
    stmt = stmt.order_by(
        Conversation.last_message_at.desc().nullslast(),
        Conversation.updated_at.desc(),
    ).limit(250)
    return list(db.scalars(stmt))


def _inbox_counts(db: Session, user: User) -> dict[str, int]:
    scoped = list(db.scalars(apply_conversation_scope(select(Conversation), user)))
    return {
        "active": sum(1 for item in scoped if item.status in OPEN_CONVERSATION_STATUSES),
        "unread": sum(1 for item in scoped if item.unread_count > 0),
        "closed": sum(1 for item in scoped if item.status in CLOSED_CONVERSATION_STATUSES),
        "queue": sum(1 for item in scoped if item.assigned_user_id is None),
    }


def _render_inbox(
    request: Request,
    db: Session,
    user: User,
    *,
    conversation_id: int | None = None,
    q: str = "",
    view: str = "active",
):
    conversations = _inbox_query(db, user, q=q, view=view)
    selected = None
    if conversation_id is not None:
        selected = _get_conversation(db, user, conversation_id)
        if not selected:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    elif conversations:
        selected = conversations[0]

    messages: list[Message] = []
    users: list[User] = []
    selected_queue = None
    latest_ai_run = None
    if selected:
        selected_queue = queue_for_conversation(db, selected.id)
        latest_ai_run = latest_run_for_conversation(db, selected.id)
        if selected.unread_count:
            mark_conversation_read(db, selected)
            db.commit()
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == selected.id)
                .options(selectinload(Message.sender_user))
                .order_by(Message.created_at.asc())
                .limit(1000)
            )
        )
        users = _eligible_users(db, user, selected)

    counts = _inbox_counts(db, user)
    return render(
        request,
        "conversations/inbox.html",
        user=user,
        active_nav="conversations",
        page_title="Conversas",
        conversations=conversations,
        conversation=selected,
        messages=messages,
        users=users,
        selected_queue=selected_queue,
        latest_ai_run=latest_ai_run,
        counts=counts,
        query=q,
        view=view,
        open_statuses=OPEN_CONVERSATION_STATUSES,
        closed_statuses=CLOSED_CONVERSATION_STATUSES,
    )


@router.get("/conversations")
def inbox(
    request: Request,
    q: str = "",
    view: str = "active",
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    return _render_inbox(request, db, user, q=q, view=view)


@router.get("/conversations/{conversation_id}")
def conversation_detail(
    conversation_id: int,
    request: Request,
    q: str = "",
    view: str = "active",
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    return _render_inbox(
        request,
        db,
        user,
        conversation_id=conversation_id,
        q=q,
        view=view,
    )


@router.post("/leads/{lead_id}/conversation")
def start_lead_conversation(
    lead_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    lead = db.scalar(apply_user_scope(select(Lead).where(Lead.id == lead_id), user))
    if not lead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not lead.phone:
        flash(request, "O lead precisa ter telefone para abrir uma conversa.", "warning")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    try:
        contact = ensure_contact(db, lead.phone, lead=lead)
    except ValueError:
        flash(request, "O telefone do lead nao e valido para WhatsApp.", "error")
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    if user.role == "CONSULTOR" and is_user_whatsapp_online(db, user.id):
        lead.assigned_user_id = user.id
        db.flush()
    conversation = ensure_open_conversation(db, contact, lead=lead, source=lead.source)
    if (
        not conversation.assigned_user_id
        and user.role == "CONSULTOR"
        and is_user_whatsapp_online(db, user.id)
    ):
        conversation.assigned_user_id = user.id
        conversation.status = "ATRIBUIDO"
        db.add(
            ConversationAssignment(
                conversation_id=conversation.id,
                user_id=user.id,
                assigned_by_id=user.id,
                event="CLAIMED",
            )
        )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/send")
def send_message(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if conversation.lead and (conversation.lead.do_not_contact or conversation.lead.stage == "NAO_CONTATAR"):
        flash(request, "Este contato esta bloqueado para novas mensagens.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    clean_message = message.strip()
    if not clean_message:
        flash(request, "Digite uma mensagem antes de enviar.", "warning")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)
    try:
        result = ZApiService().send_text(phone=conversation.contact.phone, message=clean_message)
    except ZApiError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)

    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    sent = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type="TEXT",
        body=clean_message,
        sender_user_id=user.id,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    if conversation.lead:
        conversation.lead.last_contact_at = utcnow()
        db.add(
            Activity(
                lead_id=conversation.lead.id,
                user_id=user.id,
                kind="WHATSAPP",
                status="CONCLUIDA",
                channel="WHATSAPP",
                outcome=f"CHAT_ZAPI:{provider_id or zaap_id or sent.id}"[:120],
                description=f"Mensagem enviada pelo Inbox: {clean_message}",
                completed_at=utcnow(),
            )
        )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_MESSAGE_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"message_id": sent.id, "provider_message_id": provider_id},
    )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


def _media_kind(filename: str, mime_type: str) -> str:
    clean_mime = (mime_type or "").split(";")[0].strip().lower()
    extension = Path(filename or "").suffix.lower()
    if clean_mime.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "IMAGE"
    if clean_mime.startswith("video/") or extension in {".mp4", ".mov", ".m4v", ".3gp"}:
        return "VIDEO"
    if clean_mime.startswith("audio/") or extension in {".mp3", ".ogg", ".opus", ".wav", ".m4a", ".aac", ".amr"}:
        return "AUDIO"
    return "DOCUMENT"


@router.post("/conversations/{conversation_id}/media")
async def send_media(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    media: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if conversation.lead and (
        conversation.lead.do_not_contact
        or conversation.lead.stage == "NAO_CONTATAR"
    ):
        return JSONResponse(
            {"ok": False, "error": "Este contato esta bloqueado para novas mensagens."},
            status_code=403,
        )

    original_name = Path(media.filename or "arquivo").name
    extension = Path(original_name).suffix.lower()
    blocked = {".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".sh", ".apk", ".dmg"}
    if extension in blocked:
        return JSONResponse(
            {"ok": False, "error": "Este tipo de arquivo nao e permitido."},
            status_code=400,
        )

    raw = await media.read()
    mime_type = (media.content_type or "application/octet-stream").split(";")[0].strip()
    kind = _media_kind(original_name, mime_type)

    if kind == "AUDIO":
        try:
            raw, original_name, mime_type = normalize_audio(
                raw,
                filename=original_name,
                mime_type=mime_type,
            )
        except MediaProcessingError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    storage = MediaStorage()
    try:
        storage_uri, safe_name = storage.upload_bytes(
            data=raw,
            conversation_id=conversation.id,
            filename=original_name,
            mime_type=mime_type,
            direction="OUTBOUND",
        )
        delivery_url = storage.presigned_download_url(
            storage_uri,
            filename=safe_name,
            expires_seconds=3600,
        )
    except MediaStorageError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    clean_caption = caption.strip()
    service = ZApiService()
    try:
        if kind == "IMAGE":
            result = service.send_image(
                phone=conversation.contact.phone,
                image_url=delivery_url,
                caption=clean_caption,
            )
            body = clean_caption or "[Imagem]"
        elif kind == "VIDEO":
            result = service.send_video(
                phone=conversation.contact.phone,
                video_url=delivery_url,
                caption=clean_caption,
            )
            body = clean_caption or "[Video]"
        elif kind == "AUDIO":
            result = service.send_audio(
                phone=conversation.contact.phone,
                audio_url=delivery_url,
            )
            body = "[Audio]"
        else:
            document_extension = Path(safe_name).suffix.lstrip(".") or "bin"
            result = service.send_document(
                phone=conversation.contact.phone,
                document_url=delivery_url,
                extension=document_extension,
                file_name=safe_name,
                caption=clean_caption,
            )
            body = safe_name
    except ZApiError as exc:
        failed = register_message(
            db,
            conversation,
            direction="OUTBOUND",
            message_type=kind,
            body=clean_caption or safe_name,
            sender_user_id=user.id,
            media_url=storage_uri,
            mime_type=mime_type,
            status="FAILED",
        )
        write_audit(
            db,
            user=user,
            action="CONVERSATION_MEDIA_FAILED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={
                "message_id": failed.id,
                "type": kind,
                "file_name": safe_name,
                "size_bytes": len(raw),
            },
        )
        db.commit()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    provider_id = str(result.get("messageId") or result.get("id") or "").strip() or None
    zaap_id = str(result.get("zaapId") or "").strip() or None
    sent = register_message(
        db,
        conversation,
        direction="OUTBOUND",
        message_type=kind,
        body=body,
        sender_user_id=user.id,
        media_url=storage_uri,
        mime_type=mime_type,
        provider_message_id=provider_id,
        provider_zaap_id=zaap_id,
        status="SENT",
    )
    if conversation.lead:
        conversation.lead.last_contact_at = utcnow()
        db.add(
            Activity(
                lead_id=conversation.lead.id,
                user_id=user.id,
                kind="WHATSAPP",
                status="CONCLUIDA",
                channel="WHATSAPP",
                outcome=f"CHAT_MEDIA_ZAPI:{provider_id or zaap_id or sent.id}"[:120],
                description=f"{kind} enviado pelo Inbox: {safe_name}",
                completed_at=utcnow(),
            )
        )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_MEDIA_SENT",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={
            "message_id": sent.id,
            "provider_message_id": provider_id,
            "type": kind,
            "file_name": safe_name,
            "size_bytes": len(raw),
        },
    )
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "message_id": sent.id,
            "provider_message_id": provider_id,
            "type": kind,
        }
    )


@router.get("/conversations/messages/{message_id}/media")
def conversation_media(
    message_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        return RedirectResponse("/dashboard", status_code=303)
    message = db.get(Message, message_id)
    if not message or not message.media_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    conversation = _get_conversation(db, user, message.conversation_id)
    if not conversation or not conversation_visible_to_user(conversation, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        url = MediaStorage().presigned_download_url(
            message.media_url,
            filename=message.body if message.message_type == "DOCUMENT" else None,
            expires_seconds=900,
        )
    except MediaStorageError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return RedirectResponse(url, status_code=307)


@router.post("/conversations/{conversation_id}/note")
def add_internal_note(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    note: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    clean_note = note.strip()
    if clean_note:
        message = register_message(
            db,
            conversation,
            direction="INTERNAL",
            message_type="NOTE",
            body=clean_note,
            sender_user_id=user.id,
            status="INTERNAL",
        )
        write_audit(
            db,
            user=user,
            action="CONVERSATION_NOTE_ADDED",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={"message_id": message.id},
        )
        db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/assign")
def assign_conversation(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    assigned_user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(user, "ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR")
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    eligible = {item.id: item for item in _eligible_users(db, user, conversation)}
    target = eligible.get(assigned_user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    previous = conversation.assigned_user_id
    conversation.assigned_user_id = target.id
    conversation.status = "ATRIBUIDO" if conversation.status == "EM_FILA" else conversation.status
    if conversation.lead:
        conversation.lead.assigned_user_id = target.id
    db.add(
        ConversationAssignment(
            conversation_id=conversation.id,
            user_id=target.id,
            assigned_by_id=user.id,
            event="TRANSFERRED" if previous else "ASSIGNED",
        )
    )
    write_audit(
        db,
        user=user,
        action="CONVERSATION_ASSIGNED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"from_user_id": previous, "to_user_id": target.id},
    )
    db.commit()
    flash(request, f"Conversa transferida para {target.name}.")
    if user.role == "CONSULTOR":
        return RedirectResponse("/conversations?view=active", status_code=303)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/status")
def update_conversation_status(
    conversation_id: int,
    request: Request,
    csrf_token: str = Form(...),
    conversation_status: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _user_or_redirect(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    allowed = {
        "EM_ATENDIMENTO",
        "AGUARDANDO_CLIENTE",
        "AGUARDANDO_INTERNO",
        "FOLLOW_UP",
        "RESOLVIDO",
        "ENCERRADO",
    }
    if conversation_status not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    conversation.status = conversation_status
    conversation.closed_at = utcnow() if conversation_status in CLOSED_CONVERSATION_STATUSES else None
    write_audit(
        db,
        user=user,
        action="CONVERSATION_STATUS_CHANGED",
        entity_type="conversation",
        entity_id=conversation.id,
        payload={"status": conversation_status},
    )
    db.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.get("/api/conversations/inbox")
def conversation_inbox_api(
    request: Request,
    q: str = "",
    view: str = "active",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entre no WhatsApp para acessar o canal.",
        )
    conversations = _inbox_query(db, user, q=q, view=view)
    return {
        "counts": _inbox_counts(db, user),
        "items": [
            {
                "id": item.id,
                "name": (
                    item.contact.business_name
                    or item.contact.name
                    or item.contact.phone
                ),
                "phone": item.contact.phone,
                "preview": item.last_message_preview or "Conversa iniciada",
                "owner": item.assigned_user.name if item.assigned_user else "Fila sem responsável",
                "status": item.status,
                "unread_count": int(item.unread_count or 0),
                "last_message_at": item.last_message_at.isoformat() if item.last_message_at else None,
            }
            for item in conversations
        ],
    }


@router.get("/api/conversations/{conversation_id}/messages")
def conversation_messages_api(
    conversation_id: int,
    request: Request,
    after_id: int = 0,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if user.role == "CONSULTOR" and not is_user_whatsapp_online(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entre no WhatsApp para acessar o canal.",
        )
    conversation = _get_conversation(db, user, conversation_id)
    if not conversation or not conversation_visible_to_user(conversation, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.id > max(after_id, 0),
        )
        .options(selectinload(Message.sender_user))
        .order_by(Message.id.asc())
        .limit(250)
    )
    items = list(db.scalars(stmt))
    if conversation.unread_count:
        mark_conversation_read(db, conversation)
        db.commit()
    return {
        "conversation_id": conversation.id,
        "items": [
            {
                "id": item.id,
                "direction": item.direction,
                "type": item.message_type,
                "body": item.body,
                "media_url": f"/conversations/messages/{item.id}/media" if item.media_url else None,
                "mime_type": item.mime_type,
                "status": item.status,
                "sender": item.sender_user.name if item.sender_user else ("Cliente" if item.direction == "INBOUND" else "Sulnet"),
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ],
    }
