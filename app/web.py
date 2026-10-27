from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.config import settings

from app.constants import (
    ACTIVITY_KINDS,
    ACTIVITY_STATUSES,
    CHANNELS,
    ROLES,
    SOURCES,
    STAGES,
    TEAMS,
    TEMPERATURES,
)
from app.security import csrf_token, user_to_session_context
from app.timeutils import utc_to_local


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def brl(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    formatted = f"{number:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def datetime_br(value: datetime | None, fallback: str = "—") -> str:
    if not value:
        return fallback
    local = utc_to_local(value)
    return local.strftime("%d/%m/%Y %H:%M") if local else fallback


def date_br(value: datetime | None, fallback: str = "—") -> str:
    if not value:
        return fallback
    local = utc_to_local(value)
    return local.strftime("%d/%m/%Y") if local else fallback


def phone_br(value: str | None) -> str:
    if not value:
        return "—"
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("55"):
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return value


templates.env.filters["brl"] = brl
templates.env.filters["datetime_br"] = datetime_br
templates.env.filters["date_br"] = date_br
templates.env.filters["phone_br"] = phone_br


def flash(request: Request, message: str, category: str = "success") -> None:
    items = list(request.session.get("flashes", []))
    items.append({"message": message, "category": category})
    request.session["flashes"] = items[-5:]


def consume_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flashes", [])


def render(
    request: Request,
    template_name: str,
    *,
    user=None,
    active_nav: str = "",
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    base = {
        "request": request,
        "current_user": user_to_session_context(user),
        "active_nav": active_nav,
        "csrf_token": csrf_token(request),
        "show_demo_credentials": not settings.is_production,
        "flashes": consume_flashes(request),
        "constants": {
            "roles": ROLES,
            "teams": TEAMS,
            "sources": SOURCES,
            "channels": CHANNELS,
            "stages": STAGES,
            "activity_kinds": ACTIVITY_KINDS,
            "activity_statuses": ACTIVITY_STATUSES,
            "temperatures": TEMPERATURES,
        },
    }
    base.update(context)
    return templates.TemplateResponse(request, template_name, base, status_code=status_code)
