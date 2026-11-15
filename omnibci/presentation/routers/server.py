"""Versioned Server handshake, browser sessions, and read-only diagnostics."""
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.security import SESSION_COOKIE

try:
    APP_VERSION = version("omnibci")
except PackageNotFoundError:
    APP_VERSION = "1.0.0"

router = APIRouter(prefix="/server", tags=["Server"])


class ServerInfoResponse(BaseModel):
    app_version: str
    desktop_api_version: Literal[1] = 1
    instance_id: str
    auth_enabled: bool


class ServerLogsResponse(BaseModel):
    filename: str
    lines: list[str]
    truncated: bool


@router.get("/info", response_model=ApiResponse[ServerInfoResponse])
def server_info(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=ServerInfoResponse(
        app_version=APP_VERSION,
        instance_id=request.app.state.instance_id,
        auth_enabled=request.app.state.auth_enabled,
    ))


@router.post("/session", response_model=ApiResponse[ServerInfoResponse])
def create_browser_session(request: Request, response: Response):
    if request.app.state.auth_enabled:
        response.set_cookie(
            SESSION_COOKIE, request.app.state.session_token, path="/api/v1",
            httponly=True, secure=request.url.scheme == "https", samesite="strict",
        )
    return server_info(request, response)


@router.delete("/session", response_model=ApiResponse[None])
def delete_browser_session(request: Request, response: Response):
    response.delete_cookie(
        SESSION_COOKIE, path="/api/v1", httponly=True,
        secure=request.url.scheme == "https", samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse()


@router.get("/logs", response_model=ApiResponse[ServerLogsResponse])
def server_logs(request: Request, response: Response):
    logging = getattr(request.app.state, "server_logging", None)
    if logging is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Server logs are unavailable")
    try:
        lines, truncated = logging.read_tail()
    except OSError as exc:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Server logs are unavailable") from exc
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=ServerLogsResponse(
        filename=logging.file_path.name, lines=lines, truncated=truncated,
    ))
