"""Primeiro acesso de usuário com senha temporária num perfil que exige MFA.

Regressão: /change-password redirecionava para /mfa/setup, que redirecionava de
volta para /change-password (ERR_TOO_MANY_REDIRECTS).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import security
from app.config import settings


class _Db:
    def __init__(self, user):
        self.user = user

    def get(self, _model, _id):
        return self.user


def _request(path):
    return SimpleNamespace(
        session={"user_id": 7, "session_version": 1},
        url=SimpleNamespace(path=path),
    )


def _user(**overrides):
    values = dict(
        id=7,
        active=True,
        session_version=1,
        role="GESTOR",
        must_change_password=True,
        mfa_enabled=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _location(path, user):
    try:
        security.get_current_user(_request(path), _Db(user))
    except HTTPException as exc:
        return exc.headers["Location"]
    return None


@pytest.fixture
def mfa_for_gestor(monkeypatch):
    monkeypatch.setattr(type(settings), "required_mfa_roles", property(lambda self: {"GESTOR"}))


def test_temporary_password_page_does_not_redirect_to_mfa(mfa_for_gestor):
    assert _location("/change-password", _user()) is None


def test_mfa_setup_sends_back_to_password_first(mfa_for_gestor):
    assert _location("/mfa/setup", _user()) == "/change-password"


def test_after_password_change_mfa_is_required(mfa_for_gestor):
    user = _user(must_change_password=False)

    assert _location("/dashboard", user) == "/mfa/setup"
    assert _location("/mfa/setup", user) is None
