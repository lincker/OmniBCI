"""Versioned Server handshake and read-only server diagnostics."""
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode

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
def server_info(request: Request):
    return ApiResponse(data=ServerInfoResponse(
        app_version=APP_VERSION,
        instance_id=request.app.state.instance_id,
        auth_enabled=request.app.state.auth_enabled,
    ))


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
