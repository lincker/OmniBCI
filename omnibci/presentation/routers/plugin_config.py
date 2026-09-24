"""Schema-driven profiles; saving never silently activates changed values."""
from contextlib import contextmanager
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request

from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.plugin_profiles import (
    PluginProfileNotFoundError, PluginProfileStorageError, PluginProfileValidationError,
    PluginSelection, ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import (
    AcceptActivationRequest, ActivationResponse, CreatePluginProfileRequest,
    PluginConfigurationResponse, PluginKind, PluginProfileResponse, UpdatePluginProfileRequest,
)

router = APIRouter(prefix="/config", tags=["Plugin configuration"])


def get_plugin_profile_service(request: Request) -> PluginProfileService:
    container = getattr(request.app.state, "container", None)
    service = getattr(container, "plugin_profile_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin configuration is unavailable")
    return service


@contextmanager
def _configuration_errors():
    try:
        yield
    except (PluginSchemaError, PluginProfileValidationError) as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except (ProfileRevisionConflict, ConfigProfileConflictError) as exc:
        raise ApiError(409, ErrorCode.CONFLICT, str(exc)) from exc
    except PluginProfileNotFoundError as exc:
        raise ApiError(404, ErrorCode.CONFIG_PROFILE_NOT_FOUND, f"Profile not found: {exc}") from exc
    except PluginProfileStorageError as exc:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/plugin-catalog/{kind}", response_model=ApiResponse[list[PluginConfigurationResponse]])
def plugin_catalog(kind: PluginKind, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=[asdict(item) for item in svc.catalog.list(kind)])


@router.get("/plugin-profiles", response_model=ApiResponse[list[PluginProfileResponse]])
def list_profiles(kind: PluginKind | None = None,
                  svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=[item.to_dict() for item in svc.repository.list_profiles(kind)])


@router.post("/plugin-profiles", status_code=201, response_model=ApiResponse[PluginProfileResponse])
def create_profile(body: CreatePluginProfileRequest,
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        selection = PluginSelection(**body.selection.model_dump())
        profile = svc.create(body.kind, selection, body.name, body.parameters)
        return ApiResponse(message="Profile created", data=profile.to_dict())


@router.get("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def get_profile(profile_id: str, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=svc.repository.get_profile(profile_id).to_dict())


@router.put("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def update_profile(profile_id: str, body: UpdatePluginProfileRequest,
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        profile = svc.update(profile_id, **body.model_dump())
        return ApiResponse(message="Profile saved", data=profile.to_dict())


@router.delete("/plugin-profiles/{profile_id}", response_model=ApiResponse)
def delete_profile(profile_id: str, expected_revision: int = Query(ge=1),
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        svc.delete(profile_id, expected_revision=expected_revision)
        return ApiResponse(message="Profile deleted")


@router.get("/activation", response_model=ApiResponse[ActivationResponse])
def get_activation(svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        snapshot = svc.repository.get_activation()
        return ApiResponse(data=snapshot.to_dict() if snapshot is not None else None)


@router.put("/activation", response_model=ApiResponse[ActivationResponse])
def accept_activation(body: AcceptActivationRequest,
                      svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        snapshot = svc.propose([item.model_dump() for item in body.bindings],
                               settings=svc.settings_source(), refresh_slots=body.refresh_slots)
        accepted = svc.accept(snapshot, expected_revision=body.expected_revision)
        return ApiResponse(message="Configuration selected for the next start", data=accepted.to_dict())
