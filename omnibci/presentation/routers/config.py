"""Read and save host settings; plugin profiles and runtime application are separate."""

from fastapi import APIRouter, Depends, status

from omnibci.application.services.config_service import (
    ConfigKeyNotAllowedError,
    ConfigService,
    ConfigValidationError,
)
from omnibci.presentation.dependencies import get_config_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.config import (
    ConfigEntry,
    ConfigResponse,
    PatchConfigRequest,
    PatchConfigResponse,
)

router = APIRouter(prefix="/config", tags=["Config"])


@router.get("", response_model=ApiResponse[ConfigResponse], summary="Saved host settings")
def get_config(svc: ConfigService = Depends(get_config_service)):
    try:
        entries, revision = svc.read()
    except OSError as exc:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.SERVICE_UNAVAILABLE,
                       "Host settings storage is unavailable") from exc
    return ApiResponse(data=ConfigResponse(
        entries=[ConfigEntry(**entry) for entry in entries], revision=revision))


@router.patch("", response_model=ApiResponse[PatchConfigResponse],
              summary="Save host settings without applying them")
def patch_config(body: PatchConfigRequest,
                 svc: ConfigService = Depends(get_config_service)):
    try:
        saved, restart, revision = svc.save(body.updates, expected_revision=body.expected_revision)
    except ConfigKeyNotAllowedError as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, ErrorCode.CONFIG_KEY_NOT_ALLOWED,
                       "Keys outside the config whitelist", detail={"keys": exc.keys})
    except ConfigValidationError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, ErrorCode.VALIDATION_ERROR,
                       "Config value validation failed", detail=exc.bad_keys)
    except OSError as exc:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.SERVICE_UNAVAILABLE,
                       "Host settings storage is unavailable") from exc
    return ApiResponse(
        message="Configuration saved",
        data=PatchConfigResponse(saved=saved, engine_restart_required=restart, revision=revision))
