from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pyotp

from app.routers import auth
from app.routers.auth import _allowed_oidc_email
from app.services.ai_seller import _money, _money_for_api


def test_money_is_quantized_with_decimal():
    assert _money("119.90") == Decimal("119.90")
    assert _money(99.9) == Decimal("99.90")
    assert _money(None) is None
    assert _money_for_api(Decimal("119.90")) == 119.9


def test_totp_round_trip():
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    code = totp.now()
    assert totp.verify(code, valid_window=1)


def test_oidc_domain_allowlist(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        SimpleNamespace(
            oidc_allowed_domains="sulnet.com.br,empresa.example",
        ),
    )
    assert _allowed_oidc_email("rafael@sulnet.com.br") is True
    assert _allowed_oidc_email("user@empresa.example") is True
    assert _allowed_oidc_email("attacker@example.org") is False
