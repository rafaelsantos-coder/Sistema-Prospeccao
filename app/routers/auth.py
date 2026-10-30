from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import User
from app.services.audit import write_audit
from app.services.auth_throttle import (
    clear_failures,
    record_failure,
    retry_after_seconds,
)
from app.services.whatsapp_presence import leave_whatsapp
from app.security import (
    get_current_user,
    hash_password,
    login_user,
    logout_user,
    password_needs_rehash,
    validate_csrf,
    verify_password,
)
from app.web import flash, render


router = APIRouter()


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        destination = "/change-password" if user.must_change_password else "/dashboard"
        return RedirectResponse(destination, status_code=303)
    return render(request, "login.html", page_title="Entrar")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    clean_email = email.strip().lower()
    client_ip = request.client.host if request.client else None
    retry_after = retry_after_seconds(
        db,
        email=clean_email,
        ip=client_ip,
    )
    if retry_after:
        write_audit(
            db,
            user=None,
            action="LOGIN_RATE_LIMITED",
            entity_type="auth",
            payload={
                "email": clean_email,
                "ip": client_ip,
                "retry_after_seconds": retry_after,
            },
        )
        db.commit()
        return render(
            request,
            "login.html",
            page_title="Entrar",
            error="Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.",
            email=email,
            status_code=429,
        )

    user = db.scalar(select(User).where(User.email == clean_email))
    if not user or not user.active or not verify_password(password, user.password_hash):
        retry_after = record_failure(
            db,
            email=clean_email,
            ip=client_ip,
        )
        write_audit(
            db,
            user=user if user and user.active else None,
            action="LOGIN_FAILED",
            entity_type="auth",
            entity_id=user.id if user else None,
            payload={
                "email": clean_email,
                "ip": client_ip,
                "retry_after_seconds": retry_after,
            },
        )
        db.commit()
        return render(
            request,
            "login.html",
            page_title="Entrar",
            error="E-mail ou senha inválidos.",
            email=email,
            status_code=422,
        )
    clear_failures(db, email=clean_email, ip=client_ip)

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        write_audit(
            db,
            user=user,
            action="PASSWORD_REHASHED_ARGON2ID",
            entity_type="user",
            entity_id=user.id,
        )

    login_user(request, user)
    leave_whatsapp(db, user)
    write_audit(
        db,
        user=user,
        action="LOGIN_SUCCESS",
        entity_type="auth",
        entity_id=user.id,
        payload={"ip": client_ip},
    )
    db.commit()
    flash(request, f"Bem-vindo, {user.name.split()[0]}.")
    destination = "/change-password" if user.must_change_password else "/dashboard"
    return RedirectResponse(destination, status_code=303)


@router.get("/change-password")
def change_password_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(
        request,
        "change_password.html",
        user=user,
        page_title="Alterar senha",
        required_change=user.must_change_password,
    )


@router.post("/change-password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)

    error = None
    if not verify_password(current_password, user.password_hash):
        error = "A senha atual está incorreta."
    elif new_password != confirm_password:
        error = "A confirmação não corresponde à nova senha."
    elif new_password == current_password:
        error = "A nova senha deve ser diferente da senha atual."
    else:
        try:
            user.password_hash = hash_password(new_password)
        except ValueError as exc:
            error = str(exc)

    if error:
        return render(
            request,
            "change_password.html",
            user=user,
            page_title="Alterar senha",
            required_change=user.must_change_password,
            error=error,
            status_code=422,
        )

    user.must_change_password = False
    user.session_version = int(user.session_version or 1) + 1
    login_user(request, user)
    write_audit(
        db,
        user=user,
        action="PASSWORD_CHANGED",
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    flash(request, "Senha alterada com segurança.")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if user:
        leave_whatsapp(db, user)
        write_audit(
            db,
            user=user,
            action="LOGOUT",
            entity_type="auth",
            entity_id=user.id,
            payload={"ip": request.client.host if request.client else None},
        )
        db.commit()
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
