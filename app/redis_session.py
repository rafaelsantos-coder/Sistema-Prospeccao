from __future__ import annotations

import json
import secrets
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from redis.asyncio import Redis
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings


class RedisSessionMiddleware:
    """Sessão server-side.

    O navegador recebe apenas um identificador aleatório assinado. O conteúdo
    (user_id, CSRF e metadados) permanece no Redis. Se o Redis estiver
    temporariamente indisponível, a requisição segue sem sessão autenticada
    em vez de liberar acesso por fallback menos seguro.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        redis_url: str,
        secret_key: str,
        cookie_name: str,
        max_age: int,
        same_site: str = "lax",
        https_only: bool = True,
    ) -> None:
        self.app = app
        self.redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self.signer = TimestampSigner(secret_key)
        self.cookie_name = cookie_name
        self.max_age = max(300, int(max_age))
        self.same_site = same_site
        self.https_only = https_only

    def _cookie_value(self, sid: str) -> str:
        return self.signer.sign(sid.encode("utf-8")).decode("utf-8")

    def _sid_from_cookie(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            unsigned = self.signer.unsign(
                value,
                max_age=self.max_age,
            )
            sid = unsigned.decode("utf-8")
        except (BadSignature, SignatureExpired, UnicodeDecodeError):
            return None
        if len(sid) < 32 or len(sid) > 128:
            return None
        return sid

    def _parse_cookie(self, scope: Scope) -> str | None:
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"cookie", b"").decode("latin-1")
        for item in raw.split(";"):
            key, sep, value = item.strip().partition("=")
            if sep and key == self.cookie_name:
                return value
        return None

    def _set_cookie_header(self, headers: MutableHeaders, value: str) -> None:
        pieces = [
            f"{self.cookie_name}={value}",
            "path=/",
            "httponly",
            f"samesite={self.same_site}",
            f"max-age={self.max_age}",
        ]
        if self.https_only:
            pieces.append("secure")
        headers.append("set-cookie", "; ".join(pieces))

    def _clear_cookie_header(self, headers: MutableHeaders) -> None:
        pieces = [
            f"{self.cookie_name}=null",
            "path=/",
            "httponly",
            f"samesite={self.same_site}",
            "max-age=0",
        ]
        if self.https_only:
            pieces.append("secure")
        headers.append("set-cookie", "; ".join(pieces))

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        existing_cookie = self._parse_cookie(scope)
        sid = self._sid_from_cookie(existing_cookie)
        session: dict[str, Any] = {}
        redis_available = True

        if sid:
            try:
                raw = await self.redis.get(f"session:{sid}")
            except Exception:
                raw = None
                redis_available = False
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        session = parsed
                except (json.JSONDecodeError, TypeError):
                    session = {}

        scope["session"] = session
        original_sid = sid

        async def send_wrapper(message: Message) -> None:
            nonlocal sid
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                current = scope.get("session")
                current = current if isinstance(current, dict) else {}

                if current and redis_available:
                    if not sid:
                        sid = secrets.token_urlsafe(32)
                    try:
                        await self.redis.setex(
                            f"session:{sid}",
                            self.max_age,
                            json.dumps(
                                current,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    except Exception:
                        # Falha fechada: não materializa uma sessão autenticada
                        # no cliente quando ela não pôde ser persistida.
                        self._clear_cookie_header(headers)
                    else:
                        self._set_cookie_header(
                            headers,
                            self._cookie_value(sid),
                        )
                elif not current:
                    if original_sid and redis_available:
                        try:
                            await self.redis.delete(
                                f"session:{original_sid}"
                            )
                        except Exception:
                            pass
                    self._clear_cookie_header(headers)

            await send(message)

        await self.app(scope, receive, send_wrapper)
