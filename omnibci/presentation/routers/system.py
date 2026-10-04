"""Versioned desktop handshake without host secrets or filesystem paths."""
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnibci.presentation.schemas.common import ApiResponse

try:
    APP_VERSION = version("omnibci")
except PackageNotFoundError:
    APP_VERSION = "1.0.0"

router = APIRouter(prefix="/system", tags=["System"])


class SystemInfoResponse(BaseModel):
    app_version: str
    desktop_api_version: Literal[1] = 1
    instance_id: str
    auth_enabled: bool


@router.get("/info", response_model=ApiResponse[SystemInfoResponse])
def system_info(request: Request):
    return ApiResponse(data=SystemInfoResponse(
        app_version=APP_VERSION,
        instance_id=request.app.state.instance_id,
        auth_enabled=request.app.state.auth_enabled,
    ))
