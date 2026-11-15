"""Shared API authentication and admission while the Server prepares to quit."""
import asyncio
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import Headers, URL
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse


SESSION_COOKIE = "omnibci_session"


def same_origin(origin: str, scope: dict) -> bool:
    """Compare browser HTTP origins against HTTP and WebSocket request URLs."""
    try:
        supplied = urlsplit(origin)
        target = URL(scope=scope)
        scheme = {"ws": "http", "wss": "https"}.get(target.scheme, target.scheme)
        return (
            supplied.scheme in {"http", "https"}
            and not (supplied.path or supplied.query or supplied.fragment or supplied.username or supplied.password)
            and (supplied.scheme, supplied.hostname, supplied.port or (443 if supplied.scheme == "https" else 80))
            == (scheme, target.hostname, target.port or (443 if scheme == "https" else 80))
        )
    except ValueError:
        return False


class RequestGate:
    """Drain accepted writes before stopping devices; reads remain available."""

    def __init__(self):
        self.prepared = False
        self.active_writes = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def prepare(self):
        self.prepared = True
        await self.idle.wait()


class ApiAccessMiddleware:
    def __init__(self, app, *, token: str | None, session_token: str, gate: RequestGate):
        self.app = app
        self.token = token
        self.session_token = session_token
        self.gate = gate

    async def reject(self, scope, receive, send, status: int, message: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await JSONResponse(
                {"success": False, "code": "UNAUTHORIZED" if status == 401 else "FORBIDDEN",
                 "message": message, "detail": None},
                status_code=status, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
            )(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        write = scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None and not same_origin(origin, scope):
            await self.reject(scope, receive, send, 403, "Requests must use the Server's own origin")
            return
        if self.token:
            supplied = headers.get("authorization")
            login = scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/api/v1/server/session"
            if supplied is not None or login:
                authenticated = secrets.compare_digest((supplied or "").encode(), f"Bearer {self.token}".encode())
            else:
                cookie = HTTPConnection(scope).cookies.get(SESSION_COOKIE, "")
                authenticated = secrets.compare_digest(cookie.encode(), self.session_token.encode())
                if authenticated and (write or scope["type"] == "websocket") and origin is None:
                    await self.reject(scope, receive, send, 403, "Browser writes and result connections require an Origin")
                    return
            if not authenticated:
                await self.reject(scope, receive, send, 401, "A valid API token or browser session is required")
                return
        if write and self.gate.prepared:
            await JSONResponse(
                {"success": False, "code": "CONFLICT", "message": "Server is preparing to shut down", "detail": None},
                status_code=409,
            )(scope, receive, send)
            return
        if write:
            self.gate.active_writes += 1
            self.gate.idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            if write:
                self.gate.active_writes -= 1
                if self.gate.active_writes == 0:
                    self.gate.idle.set()
