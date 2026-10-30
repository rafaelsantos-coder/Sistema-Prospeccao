from __future__ import annotations

import base64
import secrets
import time
from io import BytesIO

import pyotp
import qrcode
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
from app.services.secret_store import decrypt_secret, encrypt_secret
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
    if user.mfa_enabled:
        request.session.clear()
        request.session["pending_mfa_user_id"] = user.id
        request.session["pending_mfa_method"] = method
        request.session["pending_mfa_started_at"] = int(time.time())
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        write_audit(
            db,
            user=user,
            action="LOGIN_MFA_CHALLENGE",
            entity_type="auth",
            entity_id=user.id,
            payload={"ip": client_ip, "method": method},
        )
        db.commit()
        return RedirectResponse("/mfa/verify", status_code=303)

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


def _mfa_qr_data(secret: str, user: User) -> tuple[str, str]:
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name="Sulnet Prospecta",
    )
    image = qrcode.make(uri)
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return uri, f"data:image/png;base64,{encoded}"


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


@router.get("/mfa/verify")
def mfa_verify_page(request: Request, db: Session = Depends(get_db)):
    pending_id = request.session.get("pending_mfa_user_id")
    started_at = int(request.session.get("pending_mfa_started_at") or 0)
    if not pending_id or time.time() - started_at > 300:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    user = db.get(User, int(pending_id))
    if not user or not user.active or not user.mfa_enabled:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return render(
        request,
        "mfa_verify.html",
        page_title="Verificação em duas etapas",
        pending_user=user,
    )


@router.post("/mfa/verify")
def mfa_verify(
    request: Request,
    code: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    pending_id = request.session.get("pending_mfa_user_id")
    method = str(request.session.get("pending_mfa_method") or "PASSWORD")
    started_at = int(request.session.get("pending_mfa_started_at") or 0)
    if not pending_id or time.time() - started_at > 300:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    user = db.get(User, int(pending_id))
    if not user or not user.active or not user.mfa_enabled:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    try:
        secret = decrypt_secret(user.mfa_secret_encrypted)
    except Exception:
        secret = ""
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        write_audit(
            db,
            user=user,
            action="LOGIN_MFA_FAILED",
            entity_type="auth",
            entity_id=user.id,
        )
        db.commit()
        return render(
            request,
            "mfa_verify.html",
            page_title="Verificação em duas etapas",
            pending_user=user,
            error="Código inválido ou expirado.",
            status_code=422,
        )

    request.session.clear()
    login_user(request, user)
    leave_whatsapp(db, user)
    write_audit(
        db,
        user=user,
        action="LOGIN_SUCCESS",
        entity_type="auth",
        entity_id=user.id,
        payload={
            "method": method,
            "mfa": True,
            "ip": request.client.host if request.client else None,
        },
    )
    db.commit()
    flash(request, f"Bem-vindo, {user.name.split()[0]}.")
    destination = "/change-password" if user.must_change_password else "/dashboard"
    return RedirectResponse(destination, status_code=303)


@router.get("/mfa/setup")
def mfa_setup_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    secret = str(request.session.get("pending_mfa_secret") or "")
    if not user.mfa_enabled and not secret:
        secret = pyotp.random_base32()
        request.session["pending_mfa_secret"] = secret
    uri = ""
    qr_data = ""
    if secret:
        uri, qr_data = _mfa_qr_data(secret, user)
    return render(
        request,
        "mfa_setup.html",
        user=user,
        page_title="Segurança da conta",
        mfa_enabled=user.mfa_enabled,
        secret=secret,
        provisioning_uri=uri,
        qr_data=qr_data,
        mfa_required=user.role.upper() in settings.required_mfa_roles,
    )


@router.post("/mfa/setup")
def mfa_setup(
    request: Request,
    code: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    secret = str(request.session.get("pending_mfa_secret") or "")
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        return render(
            request,
            "mfa_setup.html",
            user=user,
            page_title="Segurança da conta",
            mfa_enabled=False,
            secret=secret,
            provisioning_uri=_mfa_qr_data(secret, user)[0] if secret else "",
            qr_data=_mfa_qr_data(secret, user)[1] if secret else "",
            mfa_required=user.role.upper() in settings.required_mfa_roles,
            error="Código inválido. Confira o aplicativo autenticador.",
            status_code=422,
        )
    user.mfa_secret_encrypted = encrypt_secret(secret)
    user.mfa_enabled = True
    user.session_version = int(user.session_version or 1) + 1
    request.session.pop("pending_mfa_secret", None)
    login_user(request, user)
    write_audit(
        db,
        user=user,
        action="MFA_ENABLED",
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    flash(request, "Autenticação em duas etapas ativada.")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/mfa/disable")
def mfa_disable(
    request: Request,
    current_password: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    if user.role.upper() in settings.required_mfa_roles:
        flash(request, "MFA é obrigatório para o seu perfil.", "warning")
        return RedirectResponse("/mfa/setup", status_code=303)
    if not verify_password(current_password, user.password_hash):
        flash(request, "Senha atual inválida.", "error")
        return RedirectResponse("/mfa/setup", status_code=303)
    try:
        secret = decrypt_secret(user.mfa_secret_encrypted)
    except Exception:
        secret = ""
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        flash(request, "Código de autenticação inválido.", "error")
        return RedirectResponse("/mfa/setup", status_code=303)

    user.mfa_enabled = False
    user.mfa_secret_encrypted = ""
    user.session_version = int(user.session_version or 1) + 1
    login_user(request, user)
    write_audit(
        db,
        user=user,
        action="MFA_DISABLED",
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    flash(request, "Autenticação em duas etapas desativada.", "warning")
    return RedirectResponse("/dashboard", status_code=303)


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
