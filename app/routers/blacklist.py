"""Cadastro gerencial e importação da blacklist global da Sulnet."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import GlobalContactBlacklist
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.global_blacklist import (
    BlacklistPhoneError,
    block_phone,
    parse_blacklist_csv,
    remove_phone_block,
)
from app.services.upload_security import UploadValidationError, validate_upload
from app.web import flash, render


router = APIRouter(prefix="/blacklist")


def _actor(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    require_roles(user, "ADMIN", "GESTOR")
    return user, None


@router.get("")
def blacklist_list(
    request: Request, q: str = "", show: str = "active",
    db: Session = Depends(get_db),
):
    user, redirect = _actor(request, db)
    if redirect:
        return redirect
    filters = []
    if show != "all":
        filters.append(GlobalContactBlacklist.active.is_(True))
    term = q.strip()[:80]
    if term:
        filters.append(or_(
            GlobalContactBlacklist.phone.ilike(f"%{term}%"),
            GlobalContactBlacklist.name.ilike(f"%{term}%"),
            GlobalContactBlacklist.reason.ilike(f"%{term}%"),
        ))
    entries = list(db.scalars(
        select(GlobalContactBlacklist).where(*filters)
        .order_by(GlobalContactBlacklist.active.desc(), GlobalContactBlacklist.updated_at.desc())
        .limit(300)
    ))
    total_active = db.scalar(select(func.count(GlobalContactBlacklist.id)).where(
        GlobalContactBlacklist.active.is_(True)
    )) or 0
    return render(
        request, "blacklist/index.html", user=user,
        active_nav="blacklist", entries=entries, q=term,
        show=show, total_active=total_active,
    )


@router.post("/add")
def add_blacklist_number(
    request: Request,
    csrf_token: str = Form(...),
    phone: str = Form(...),
    name: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    try:
        row, created = block_phone(
            db, phone=phone, name=name, reason=reason,
            source="GERENCIAL", actor_id=user.id,
        )
    except BlacklistPhoneError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/blacklist", status_code=303)
    write_audit(
        db, user=user, action="GLOBAL_BLACKLIST_ADDED",
        entity_type="global_contact_blacklist", entity_id=row.id,
        payload={"created": created, "source": "GERENCIAL"},
    )
    db.commit()
    flash(
        request,
        "Número incluído na blacklist de todas as filas." if created
        else "Este número já estava bloqueado globalmente.",
        "success",
    )
    return RedirectResponse("/blacklist", status_code=303)


@router.post("/import")
async def import_blacklist_numbers(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    payload = await file.read(2 * 1024 * 1024 + 1)
    try:
        validate_upload(filename=file.filename, mime_type=file.content_type, data=payload)
        rows, report = parse_blacklist_csv(payload, max_rows=10000)
    except (UploadValidationError, ValueError) as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse("/blacklist", status_code=303)

    inserted = 0
    already_blocked = 0
    for item in rows:
        row, created = block_phone(
            db, phone=item["phone"], name=item["name"],
            reason=item["reason"], source="CSV", actor_id=user.id,
        )
        if created:
            inserted += 1
        else:
            already_blocked += 1
    write_audit(
        db, user=user, action="GLOBAL_BLACKLIST_CSV_IMPORTED",
        entity_type="global_contact_blacklist",
        payload={
            "inserted": inserted,
            "already_blocked": already_blocked,
            "invalid_rows": report["invalidos"],
            "duplicate_rows": report["duplicados_arquivo"],
            "total_rows": report["linhas"],
        },
    )
    db.commit()
    flash(
        request,
        f"Importação concluída: {inserted} bloqueados, {already_blocked} já existentes, "
        f"{report['invalidos']} inválidos e {report['duplicados_arquivo']} duplicados no arquivo.",
        "success",
    )
    return RedirectResponse("/blacklist", status_code=303)


@router.post("/{entry_id}/remove")
def remove_blacklist_number(
    entry_id: int,
    request: Request,
    csrf_token: str = Form(...),
    confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _actor(request, db)
    if redirect:
        return redirect
    require_roles(user, "ADMIN")
    validate_csrf(request, csrf_token)
    if confirmation != "DESBLOQUEAR":
        flash(request, "Confirme digitando DESBLOQUEAR.", "warning")
        return RedirectResponse("/blacklist", status_code=303)
    entry = db.get(GlobalContactBlacklist, entry_id)
    if entry is None:
        flash(request, "Contato não encontrado.", "warning")
        return RedirectResponse("/blacklist", status_code=303)
    if entry.active:
        remove_phone_block(db, entry=entry, actor_id=user.id)
        write_audit(
            db, user=user, action="GLOBAL_BLACKLIST_REMOVED",
            entity_type="global_contact_blacklist", entity_id=entry.id,
            payload={"source": "GERENCIAL"},
        )
        db.commit()
    flash(request, "Bloqueio global desativado por administrador.", "success")
    return RedirectResponse("/blacklist", status_code=303)
