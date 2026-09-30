"""Operator token check, same-origin policy and security headers (DESIGN §15).

* Every POST needs ``Authorization: Bearer <operator token>`` (:func:`require_operator`).
* Any request (HTTP or WebSocket) that carries an ``Origin`` header not matching the server's own
  origin is rejected with 403 (:class:`OriginGuard`). Same-origin browser fetches either omit
  ``Origin`` (GET) or send the server's own origin (POST, WebSocket).
* ``GET /api/session`` hands the token only to same-origin fetches (:func:`is_same_origin_fetch`).
* No CORS headers are ever sent, so a foreign page can neither read the token nor call a POST.
* :class:`SecurityHeaders` adds the §15 Content-Security-Policy to ``/`` and ``/api/*`` responses.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from fastapi import HTTPException, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["CSP", "allowed_origins", "origin_ok", "is_same_origin_fetch", "require_operator",
           "OriginGuard", "SecurityHeaders"]

CSP = ("default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; img-src 'self' data:; connect-src 'self' ws: wss:; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")

_LOOPBACK = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"}


def allowed_origins(host: str, port: int) -> frozenset[str]:
    """``http://{host}:{port}`` plus the loopback aliases (``localhost`` <-> ``127.0.0.1``)."""
    hosts = {host.lower()}
    if host.lower() in _LOOPBACK:
        hosts |= {"127.0.0.1", "localhost", "[::1]"}
    return frozenset(f"http://{h}:{port}" for h in hosts)


def origin_ok(origin: str | None, request_host: str | None, allowed: Iterable[str]) -> bool:
    """True when ``origin`` is absent, in ``allowed``, or equals the request's own ``Host`` (any scheme).

    The Host match keeps the server usable when bound to a LAN address or behind a TLS proxy:
    a browser always sends its page's origin, so a foreign page still fails this check.
    """
    if origin is None:
        return True
    o = origin.strip().rstrip("/").lower()
    if not o or o == "null":
        return False
    if o in allowed:
        return True
    if request_host:
        h = request_host.strip().lower()
        return o in (f"http://{h}", f"https://{h}")
    return False


def is_same_origin_fetch(request: Request) -> bool:
    """Same-origin per the browser's own signals: no foreign ``Origin`` and ``Sec-Fetch-Site`` absent/same-origin/none."""
    allowed = request.app.state.allowed_origins
    if not origin_ok(request.headers.get("origin"), request.headers.get("host"), allowed):
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site.lower() in ("same-origin", "none")


def require_operator(request: Request) -> str:
    """FastAPI dependency for every POST: ``Authorization: Bearer <token>`` compared in constant time."""
    expected: str = request.app.state.sim.operator_token
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="operator token required", headers={"WWW-Authenticate": "Bearer"})
    return token


class OriginGuard:
    """Pure ASGI middleware: 403 (HTTP) or a pre-accept close (WebSocket) for a foreign ``Origin``."""

    def __init__(self, app: ASGIApp, allowed: Iterable[str]) -> None:
        self.app = app
        self.allowed = frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            if not origin_ok(headers.get("origin"), headers.get("host"), self.allowed):
                if scope["type"] == "http":
                    await JSONResponse({"detail": "foreign origin"}, status_code=403)(scope, receive, send)
                else:
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class SecurityHeaders:
    """Pure ASGI middleware adding the CSP (and nosniff) to ``/``, ``/api/*`` and any HTML response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        applies = path == "/" or path.startswith("/api/") or path == "/api"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(raw=message["headers"])
                if applies or headers.get("content-type", "").startswith("text/html"):
                    headers["content-security-policy"] = CSP
                    headers["x-content-type-options"] = "nosniff"
                    headers["referrer-policy"] = "same-origin"
            await send(message)

        await self.app(scope, receive, send_wrapper)
