from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.models import User
from app.services.audit import write_audit
from app.security import (
    get_current_user,
    hash_password,
    login_user,
    logout_user,
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
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        write_audit(
            db,
            user=user if user and user.active else None,
            action="LOGIN_FAILED",
            entity_type="auth",
            entity_id=user.id if user else None,
            payload={"email": email.strip().lower(), "ip": request.client.host if request.client else None},
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
    login_user(request, user)
    write_audit(
        db,
        user=user,
        action="LOGIN_SUCCESS",
        entity_type="auth",
        entity_id=user.id,
        payload={"ip": request.client.host if request.client else None},
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
    write_audit(db, user=user, action="PASSWORD_CHANGED", entity_type="user", entity_id=user.id)
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
