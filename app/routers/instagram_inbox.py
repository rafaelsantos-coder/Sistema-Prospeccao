"""Independent HTML inbox and membership settings, default disabled."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from app.database import get_db
from app.models import User
from app.models_instagram import instagram_inbox_members as members
from app.security import csrf_token, require_permission, require_roles, require_user, validate_csrf
from app.services.access_control import has_permission
from app.services import instagram_inbox as inbox

router = APIRouter()
ROLES = ("CONSULTOR", "SUPERVISOR", "GESTOR", "ADMIN")
QUEUE = "Instagram – Digital"


def account():
    # Local import avoids circular import with the existing webhook router.
    from app.routers.instagram import _account_id
    if not inbox.enabled():
        raise HTTPException(status_code=503, detail="Fila Instagram ainda não ativada.")
    return _account_id()


def manager(user):
    return user.role in ("ADMIN", "GESTOR") and has_permission(user, "settings.users.manage")


def access(request, db, *, require_member=False):
    user = require_user(request, db)
    require_roles(user, *ROLES)
    current = account()
    if (require_member or not manager(user)) and not inbox.is_member(db, current, user.id):
        raise HTTPException(status_code=403, detail="Sem vínculo com a fila Instagram.")
    return user, current


def esc(value):
    return escape(str(value), quote=True)


def render(body):
    return HTMLResponse(
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Instagram – Digital | Prospecta</title></head><body>'
        '<main><h1>Instagram – Digital</h1><p>Canal independente do WhatsApp.</p>'
        '<p><a href="/dashboard">Voltar ao Prospecta</a> · '
        '<a href="/instagram/inbox">Caixa de entrada</a></p>' + body +
        '</main></body></html>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/instagram/inbox", include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)):
    user, current = access(request, db)
    can_claim = inbox.is_member(db, current, user.id)
    token = esc(csrf_token(request))
    body = ['<p>Recebimento e distribuição em validação. Responder e registrar CRM ainda não disponíveis.</p>']
    if manager(user):
        body.append('<p><a href="/instagram/members">Gerenciar integrantes do Instagram</a></p>')
    body.append('<h2>Conversas aguardando e atribuídas</h2><ul>')
    for row in inbox.list_threads(db, current, user.id, manager(user)):
        key = esc(row["thread_id"])
        label = "Conversa " + key[:12]
        owner = row["assigned_user_id"]
        body.append('<li>' + label + ' — ' + esc(row["last_inbound_at"]) + ' UTC ')
        if owner is None and can_claim:
            body.append(f'<form method="post" action="/instagram/inbox/{key}/claim">'
                f'<input type="hidden" name="csrf" value="{token}"><button>Assumir</button></form>')
        elif owner is None:
            body.append('Aguardando vendedor')
        if owner == user.id or manager(user):
            body.append(f'<a href="/instagram/inbox/{key}">Abrir conversa</a>')
        body.append('</li>')
    body.append('</ul>')
    return render("".join(body))


@router.post("/instagram/inbox/{thread_id}/claim", include_in_schema=False)
def take(thread_id: str, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user, current = access(request, db, require_member=True)
    validate_csrf(request, csrf)
    if not inbox.claim(db, current, thread_id, user.id):
        db.rollback()
        raise HTTPException(status_code=409, detail="Conversa indisponível ou já assumida.")
    db.commit()
    return RedirectResponse(f"/instagram/inbox/{thread_id}", status_code=303)


@router.get("/instagram/inbox/{thread_id}", include_in_schema=False)
def detail(thread_id: str, request: Request, db: Session = Depends(get_db)):
    user, current = access(request, db)
    result = inbox.read_thread(db, current, thread_id, user.id, manager(user))
    if result is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    thread, rows = result
    body = ['<h2>Histórico Instagram</h2><p>Últimas 100 mensagens recebidas.</p>']
    for stamp, text in rows:
        body.append(f'<p><strong>{esc(stamp)} UTC</strong></p><pre>{esc(text)}</pre>')
    body.append('<p>Envio e registro CRM serão disponibilizados após validação.</p>')
    return render("".join(body))


@router.get("/instagram/members", include_in_schema=False)
def member_list(request: Request, db: Session = Depends(get_db)):
    user, current = access(request, db)
    if not manager(user):
        raise HTTPException(status_code=403, detail="Apenas administração do Instagram.")
    token = esc(csrf_token(request))
    active_users = db.execute(select(User).where(
        User.active.is_(True), User.role.in_(ROLES)
    ).order_by(User.name).limit(500)).scalars().all()
    existing = set(db.execute(select(members.c.user_id).where(
        members.c.account_id == current)).scalars().all())
    body = ['<h2>Integrantes exclusivos do Instagram</h2><ul>']
    for person in active_users:
        active = person.id in existing
        body.append(f'<li>{esc(person.name)} — {esc(person.role)} '
            f'<form method="post" action="/instagram/members/{person.id}">'
            f'<input type="hidden" name="csrf" value="{token}">'
            f'<input type="hidden" name="action" value="{"remove" if active else "add"}">'
            f'<button>{"Revogar" if active else "Autorizar"}</button></form></li>')
    body.append('</ul><p>Revogar acesso não transfere históricos automaticamente.</p>')
    return render("".join(body))


@router.post("/instagram/members/{user_id}", include_in_schema=False)
def member_update(user_id: int, request: Request, action: str = Form(...),
                  csrf: str = Form(...), db: Session = Depends(get_db)):
    user, current = access(request, db)
    if not manager(user):
        raise HTTPException(status_code=403, detail="Apenas administração do Instagram.")
    validate_csrf(request, csrf)
    if action == "add":
        target = db.get(User, user_id)
        if not target or not target.active or target.role not in ROLES:
            raise HTTPException(status_code=400, detail="Usuário inválido.")
        db.execute(inbox.insert_for(db)(members).values(
            account_id=current, user_id=user_id
        ).on_conflict_do_nothing(index_elements=["account_id", "user_id"]))
    elif action == "remove":
        db.execute(delete(members).where(
            members.c.account_id == current, members.c.user_id == user_id
        ))
    else:
        raise HTTPException(status_code=400, detail="Ação inválida.")
    db.commit()
    return RedirectResponse("/instagram/members", status_code=303)
