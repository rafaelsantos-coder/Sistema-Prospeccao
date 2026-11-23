from __future__ import annotations


from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import settings
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
    rehash_legacy_password,
    validate_csrf,
    verify_password,
)
from app.web import flash, render


router = APIRouter()
oauth = OAuth()
if settings.oidc_configured:
    oauth.register(
        name="corporate",
        server_metadata_url=settings.oidc_metadata_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )


def _allowed_oidc_email(email: str) -> bool:
    domains = {
        item.strip().casefold().lstrip("@")
        for item in settings.oidc_allowed_domains.split(",")
        if item.strip()
    }
    if not domains:
        return True
    domain = email.rsplit("@", 1)[-1].casefold() if "@" in email else ""
    return domain in domains


def _start_or_complete_login(
    request: Request,
    db: Session,
    user: User,
    *,
    client_ip: str | None,
    method: str,
):
    # Login exclusivo por credenciais existentes: MFA legado nao cria desafio.
    login_user(request, user)
    leave_whatsapp(db, user)
    write_audit(
        db,
        user=user,
        action="LOGIN_SUCCESS",
        entity_type="auth",
        entity_id=user.id,
        payload={"ip": client_ip, "method": method},
    )
    db.commit()
    flash(request, f"Bem-vindo, {user.name.split()[0]}.")
    destination = "/change-password" if user.must_change_password else "/dashboard"
    return RedirectResponse(destination, status_code=303)


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
        user.password_hash = rehash_legacy_password(password)
        write_audit(
            db,
            user=user,
            action="PASSWORD_REHASHED_ARGON2ID",
            entity_type="user",
            entity_id=user.id,
        )

    return _start_or_complete_login(
        request,
        db,
        user,
        client_ip=client_ip,
        method="PASSWORD",
    )


@router.get("/auth/sso")
async def oidc_login(request: Request):
    if not settings.oidc_configured:
        return RedirectResponse("/login", status_code=303)
    client = oauth.create_client("corporate")
    if not client:
        return RedirectResponse("/login", status_code=303)
    redirect_uri = str(request.url_for("oidc_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/sso/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    if not settings.oidc_configured:
        return RedirectResponse("/login", status_code=303)
    client = oauth.create_client("corporate")
    if not client:
        return RedirectResponse("/login", status_code=303)
    try:
        token = await client.authorize_access_token(request)
    except Exception:
        write_audit(
            db,
            user=None,
            action="LOGIN_SSO_FAILED",
            entity_type="auth",
            payload={"reason": "oidc_exchange_failed"},
        )
        db.commit()
        return render(
            request,
            "login.html",
            page_title="Entrar",
            error="Não foi possível concluir o login corporativo.",
            status_code=401,
        )

    info = token.get("userinfo") or {}
    if not info:
        try:
            info = await client.userinfo(token=token)
        except Exception:
            info = {}
    email = str(info.get("email") or "").strip().casefold()
    if not email or not _allowed_oidc_email(email):
        write_audit(
            db,
            user=None,
            action="LOGIN_SSO_FAILED",
            entity_type="auth",
            payload={"email": email, "reason": "domain_or_email"},
        )
        db.commit()
        return render(
            request,
            "login.html",
            page_title="Entrar",
            error="Esta conta corporativa não está autorizada.",
            status_code=403,
        )

    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.active:
        write_audit(
            db,
            user=None,
            action="LOGIN_SSO_FAILED",
            entity_type="auth",
            payload={"email": email, "reason": "user_not_preprovisioned"},
        )
        db.commit()
        return render(
            request,
            "login.html",
            page_title="Entrar",
            error="Seu usuário precisa estar previamente cadastrado no Prospecta.",
            status_code=403,
        )

    return _start_or_complete_login(
        request,
        db,
        user,
        client_ip=request.client.host if request.client else None,
        method="OIDC",
    )


@router.api_route("/mfa/verify", methods=["GET", "POST"])
def retired_mfa_verify(request: Request):
    # Desafios pendentes de antes da mudanca nao devem autenticar ninguem.
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.api_route("/mfa/setup", methods=["GET", "POST"])
@router.post("/mfa/disable")
def retired_mfa_settings(request: Request, db: Session = Depends(get_db)):
    # Rotas antigas ficam inertes. Mantemos os dados historicos no banco,
    # sem exigir ou permitir nova configuracao de codigo no login.
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
