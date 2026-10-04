"""API authentication and admission while the desktop prepares to quit."""
import asyncio
import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


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
    def __init__(self, app, *, token: str | None, gate: RequestGate):
        self.app = app
        self.token = token
        self.gate = gate

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        if self.token:
            supplied = Headers(scope=scope).get("authorization", "")
            if not secrets.compare_digest(supplied.encode(), f"Bearer {self.token}".encode()):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse(
                        {"success": False, "code": "UNAUTHORIZED", "message": "A valid API token is required", "detail": None},
                        status_code=401, headers={"WWW-Authenticate": "Bearer"},
                    )(scope, receive, send)
                return
        write = scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if write and self.gate.prepared:
            await JSONResponse(
                {"success": False, "code": "CONFLICT", "message": "Backend is preparing to shut down", "detail": None},
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
