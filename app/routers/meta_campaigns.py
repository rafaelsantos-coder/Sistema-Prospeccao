"""Gestão de templates exclusivamente via WhatsApp Business Management API."""
from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.database import get_db
from app.security import get_current_user, require_roles, validate_csrf
from app.services.meta_whatsapp import (
    default_queue_for_meta_account, is_digital_queue, is_official_digital,
    list_meta_accounts, service_for_meta_account,
)
from app.web import flash, render

router = APIRouter(prefix="/meta-campaigns")
PAGE = "/meta-campaigns"


def _actor(request, db):
    user = get_current_user(request, db)
    if not user:
        return None
    require_roles(user, "ADMIN", "GESTOR")
    return user


def _accounts(db):
    return [
        a for a in list_meta_accounts(db)
        if is_official_digital(a)
        and (q := default_queue_for_meta_account(db, a.id)) is not None
        and is_digital_queue(q)
    ]


def _account(db, account_id):
    return next((a for a in _accounts(db) if a.id == account_id), None)


def _graph(account, method, *, params=None, payload=None):
    # Never follow Graph's arbitrary paging URLs or expose tokens in error output.
    waba = (account.waba_id or "").strip()
    version = (account.graph_api_version or "").strip()
    if not re.fullmatch(r"[0-9]+", waba) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
        raise ValueError("Configure WABA ID e versão da Graph API no número oficial.")
    service = service_for_meta_account(account)
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.request(
                method, f"https://graph.facebook.com/{version}/{waba}/message_templates",
                headers={"Authorization": f"Bearer {service.access_token}"},
                params=params, json=payload,
            )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Não foi possível consultar a Meta. Verifique a conexão.") from exc
    if not isinstance(data, dict):
        raise ValueError("Resposta inesperada da Meta.")
    if response.status_code >= 300 or "error" in data:
        error = data.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise ValueError(f"Meta recusou a operação (HTTP {response.status_code}, código {code}). Confira permissões e WABA.")
    return data


def _list(account):
    rows = []
    after = None
    for _ in range(20):
        params = {"fields": "id,name,language,status,category,components", "limit": 100}
        if after:
            params["after"] = after
        data = _graph(account, "GET", params=params)
        page = data.get("data")
        if not isinstance(page, list):
            raise ValueError("Lista de templates inválida.")
        rows.extend(item for item in page if isinstance(item, dict))
        paging = data.get("paging") or {}
        if not paging.get("next"):
            return rows
        cursor = (paging.get("cursors") or {}).get("after")
        if not cursor or cursor == after:
            raise ValueError("Paginação de templates incompleta.")
        after = cursor
    raise ValueError("Limite de consulta atingido; lista incompleta.")


def build_template(name, category, body, examples="", header="", footer="", buttons=""):
    name, body = name.strip(), body.strip()
    category = category.strip().upper()
    if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
        raise ValueError("Nome: use somente letras minúsculas, números e sublinhados.")
    if category not in {"MARKETING", "UTILITY"}:
        raise ValueError("Categoria inválida.")
    if not 1 <= len(body) <= 1024:
        raise ValueError("Mensagem deve ter de 1 a 1024 caracteres.")
    variables = sorted(set(int(v) for v in re.findall(r"\{\{([1-9][0-9]*)\}\}", body)))
    remainder = re.sub(r"\{\{[1-9][0-9]*\}\}", "", body)
    if "{" in remainder or "}" in remainder:
        raise ValueError("Use variáveis numéricas como {{1}}.")
    values = [v.strip() for v in examples.splitlines() if v.strip()]
    if variables != list(range(1, len(variables) + 1)) or len(values) != len(variables):
        raise ValueError("Variáveis devem ser sequenciais; informe um exemplo por linha.")
    components = []
    header, footer = header.strip(), footer.strip()
    for text in (header, footer):
        if len(text) > 60 or "{" in text or "}" in text:
            raise ValueError("Cabeçalho e rodapé: até 60 caracteres, sem variáveis.")
    if header:
        components.append({"type": "HEADER", "format": "TEXT", "text": header})
    item = {"type": "BODY", "text": body}
    if values:
        item["example"] = {"body_text": [values]}
    components.append(item)
    if footer:
        components.append({"type": "FOOTER", "text": footer})
    labels = [v.strip() for v in buttons.splitlines() if v.strip()]
    if len(labels) > 3 or any(len(v) > 25 for v in labels) or len(set(labels)) != len(labels):
        raise ValueError("Até três botões distintos de resposta rápida, com até 25 caracteres.")
    if labels:
        components.append({"type": "BUTTONS", "buttons": [
            {"type": "QUICK_REPLY", "text": v} for v in labels
        ]})
    return {"name": name, "category": category, "language": "pt_BR", "components": components}


@router.get("")
def templates_page(request: Request, account_id: int | None = None, db: Session = Depends(get_db)):
    user = _actor(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    accounts = _accounts(db)
    account = _account(db, account_id) if account_id is not None else (accounts[0] if accounts else None)
    rows, error = [], ""
    if account_id is not None and not account:
        raise HTTPException(status_code=404)
    if account:
        try:
            rows = _list(account)
        except ValueError as exc:
            error = str(exc)
    return render(request, "meta_campaigns.html", user=user, active_nav="meta_campaigns",
                  page_title="Campanhas Meta Oficial", accounts=accounts, account=account,
                  templates=rows, meta_error=error)


@router.post("/templates")
def submit_template(request: Request, csrf_token: str = Form(...), account_id: int = Form(...),
                    name: str = Form(...), category: str = Form(...), body: str = Form(...),
                    examples: str = Form(""), header: str = Form(""), footer: str = Form(""),
                    buttons: str = Form(""), db: Session = Depends(get_db)):
    user = _actor(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    validate_csrf(request, csrf_token)
    account = _account(db, account_id)
    if not account:
        raise HTTPException(status_code=404)
    try:
        payload = build_template(name, category, body, examples, header, footer, buttons)
        result = _graph(account, "POST", payload=payload)
        if not result.get("id"):
            raise ValueError("A Meta não confirmou o cadastro; consulte a lista antes de repetir.")
        flash(request, "Template enviado à Meta. Consulte o status oficial na lista.", "success")
    except ValueError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(f"{PAGE}?account_id={account.id}", status_code=303)
