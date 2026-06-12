"""
/api/v1/config — layered configuration endpoints (stage 4, §五.8).

    effective = active profile overlay ⊕ default (settings.toml)

GET  /config                          effective view per whitelist key
PATCH /config                         write defaults or a profile (all-or-nothing)
GET  /config/profiles                 list profiles
GET  /config/profiles/{name}          one profile's overrides
PUT  /config/profiles/{name}          create/replace a profile
DELETE /config/profiles/{name}        delete (409 if active)
POST /config/profiles/{name}/activate switch to a profile
POST /config/profiles/deactivate      back to defaults only
"""
from fastapi import APIRouter, Depends, status

from omnibci.application.services.config_service import (
    ConfigKeyNotAllowedError,
    ConfigService,
    ConfigValidationError,
    ProfileInUseError,
)
from omnibci.conf import ProfileNameError, ProfileNotFoundError
from omnibci.presentation.dependencies import get_config_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.config import (
    ConfigEntry,
    ConfigResponse,
    PatchConfigRequest,
    PatchConfigResponse,
    ProfileDetailResponse,
    ProfileListResponse,
    ProfileSummary,
    PutProfileRequest,
)

router = APIRouter(prefix="/config", tags=["Config"])


def _patch_errors(exc: Exception) -> ApiError:
    if isinstance(exc, ConfigKeyNotAllowedError):
        return ApiError(status.HTTP_400_BAD_REQUEST, ErrorCode.CONFIG_KEY_NOT_ALLOWED,
                        "Keys outside the config whitelist", detail={"keys": exc.keys})
    if isinstance(exc, ConfigValidationError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR,
                        "Config value validation failed", detail=exc.bad_keys)
    if isinstance(exc, ProfileNameError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR,
                        str(exc))
    raise exc


@router.get("", response_model=ApiResponse[ConfigResponse], summary="Effective configuration")
def get_config(svc: ConfigService = Depends(get_config_service)):
    entries, active = svc.list_entries()
    return ApiResponse(data=ConfigResponse(
        entries=[ConfigEntry(**e) for e in entries], active_profile=active))


@router.patch("", response_model=ApiResponse[PatchConfigResponse],
              summary="Update defaults or a profile (all-or-nothing)")
def patch_config(body: PatchConfigRequest,
                 svc: ConfigService = Depends(get_config_service)):
    try:
        applied, restart = svc.apply(body.updates, scope=body.scope,
                                     description=body.description)
    except (ConfigKeyNotAllowedError, ConfigValidationError, ProfileNameError) as exc:
        raise _patch_errors(exc)
    return ApiResponse(
        message="Configuration updated",
        data=PatchConfigResponse(applied=applied, scope=body.scope,
                                 engine_restart_required=restart))


@router.get("/profiles", response_model=ApiResponse[ProfileListResponse],
            summary="List configuration profiles")
def list_profiles(svc: ConfigService = Depends(get_config_service)):
    profiles, active = svc.list_profiles()
    return ApiResponse(data=ProfileListResponse(
        profiles=[ProfileSummary(**p) for p in profiles], active_profile=active))


# NOTE: deactivate is declared before the /{name} routes so the literal path
# wins over the parametrised one.
@router.post("/profiles/deactivate", response_model=ApiResponse,
             summary="Back to defaults only")
def deactivate_profile(svc: ConfigService = Depends(get_config_service)):
    svc.deactivate()
    return ApiResponse(message="Profile deactivated; defaults only")


@router.get("/profiles/{name}", response_model=ApiResponse[ProfileDetailResponse],
            summary="One profile's overrides")
def get_profile(name: str, svc: ConfigService = Depends(get_config_service)):
    try:
        detail = svc.profile_detail(name)
    except ProfileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.CONFIG_PROFILE_NOT_FOUND,
                       f"Profile {name!r} not found")
    except ProfileNameError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR, str(exc))
    return ApiResponse(data=ProfileDetailResponse(**detail))


@router.put("/profiles/{name}", response_model=ApiResponse[PatchConfigResponse],
            summary="Create or replace a profile")
def put_profile(name: str, body: PutProfileRequest,
                svc: ConfigService = Depends(get_config_service)):
    try:
        applied, restart = svc.put_profile(name, body.updates, body.description)
    except (ConfigKeyNotAllowedError, ConfigValidationError, ProfileNameError) as exc:
        raise _patch_errors(exc)
    return ApiResponse(
        message=f"Profile {name!r} saved",
        data=PatchConfigResponse(applied=applied, scope=name,
                                 engine_restart_required=restart))


@router.delete("/profiles/{name}", response_model=ApiResponse,
               summary="Delete a profile (409 if active)")
def delete_profile(name: str, svc: ConfigService = Depends(get_config_service)):
    try:
        svc.delete_profile(name)
    except ProfileInUseError:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.CONFIG_PROFILE_IN_USE,
                       f"Profile {name!r} is active — deactivate or switch first")
    except ProfileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.CONFIG_PROFILE_NOT_FOUND,
                       f"Profile {name!r} not found")
    except ProfileNameError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR, str(exc))
    return ApiResponse(message=f"Profile {name!r} deleted")


@router.post("/profiles/{name}/activate", response_model=ApiResponse,
             summary="Activate a profile")
def activate_profile(name: str, svc: ConfigService = Depends(get_config_service)):
    try:
        svc.activate(name)
    except ProfileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.CONFIG_PROFILE_NOT_FOUND,
                       f"Profile {name!r} not found")
    except ProfileNameError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR, str(exc))
    return ApiResponse(message=f"Profile {name!r} activated"
                               " — engine-restart keys apply on next engine start")
