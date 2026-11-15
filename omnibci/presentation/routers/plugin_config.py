"""Schema-driven profiles; saving never silently activates changed values."""
from contextlib import contextmanager
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request

from omnibci.application.activation_settings import MODULES
from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.plugin_profiles import (
    PluginProfileNotFoundError, PluginProfileStorageError, PluginProfileValidationError,
    PluginSelection, ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import (
    ConfigurationModule, ModuleActivationResponse, ModulesResponse, SystemSettingsResponse,
    ModuleHistoryResponse, ProfileHistoryResponse, RevertProfileRequest, CreatePluginProfileRequest,
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
    except (PluginSchemaError, PluginProfileValidationError, ValueError) as exc:
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
        values = {}
        if body.processing_input is not None:
            values["processing_input"] = ProcessingInput.from_dict(body.processing_input.model_dump(exclude_none=True))
        profile = svc.create(body.kind, selection, body.name, body.parameters,
                             **values)
        return ApiResponse(message="Profile created", data=profile.to_dict())


@router.get("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def get_profile(profile_id: str, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=svc.repository.get_profile(profile_id).to_dict())


@router.put("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def update_profile(profile_id: str, body: UpdatePluginProfileRequest,
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        values = body.model_dump(exclude={"processing_input"})
        if "processing_input" in body.model_fields_set:
            values["processing_input"] = (
                ProcessingInput.from_dict(body.processing_input.model_dump(exclude_none=True))
                if body.processing_input is not None else None)
        profile = svc.update(profile_id, **values)
        return ApiResponse(message="Profile saved", data=profile.to_dict())


@router.delete("/plugin-profiles/{profile_id}", response_model=ApiResponse)
def delete_profile(profile_id: str, expected_revision: int = Query(ge=1),
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        svc.delete(profile_id, expected_revision=expected_revision)
        return ApiResponse(message="Profile deleted")


def _module_response(module, activation):
    return activation.to_dict() if activation is not None else {
        "module": module, "revision": None, "settings": {}, "bindings": [],
    }


def _system_response(system):
    return system.to_dict() if system is not None else {"revision": None, "settings": {}}


@router.get("/modules", response_model=ApiResponse[ModulesResponse])
def get_modules(svc: PluginProfileService = Depends(get_plugin_profile_service)):
    modules, errors = {}, {}
    for module in MODULES:
        try:
            modules[module] = _module_response(module, svc.repository.get_module(module))
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            errors[module] = str(exc)
    try:
        system = _system_response(svc.repository.get_system())
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        system = None
        errors["system"] = str(exc)
    return ApiResponse(data={"modules": modules, "system": system, "errors": errors})


@router.get("/modules/{module}", response_model=ApiResponse[ModuleActivationResponse])
def get_module(module: ConfigurationModule, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=_module_response(module, svc.repository.get_module(module)))


@router.get("/modules/{module}/history", response_model=ApiResponse[list[ModuleHistoryResponse]])
def module_history(module: ConfigurationModule, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=[{
            "entry_id": entry.entry_id, "revision": entry.revision, "saved_at": entry.saved_at,
            "restored_from": entry.restored_from,
            "binding_summary": [f"{b.slot}: {b.selection.package_id}@{b.selection.version}/{b.selection.contribution_id}" for b in entry.activation.bindings],
        } for entry in svc.repository.list_module_history(module)])


@router.get("/modules/{module}/history/{entry_id}", response_model=ApiResponse[ModuleActivationResponse])
def module_history_entry(module: ConfigurationModule, entry_id: str,
                         svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        entry = svc.repository.get_module_history_entry(module, entry_id)
        if entry is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Module history entry not found")
        return ApiResponse(data=entry.activation.to_dict())


@router.get("/system", response_model=ApiResponse[SystemSettingsResponse])
def get_system(svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=_system_response(svc.repository.get_system()))


@router.get("/plugin-profiles/{profile_id}/history", response_model=ApiResponse[list[ProfileHistoryResponse]])
def profile_history(profile_id: str, svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        return ApiResponse(data=[entry.to_dict() for entry in svc.profile_history(profile_id)])


@router.post("/plugin-profiles/{profile_id}/revert", response_model=ApiResponse[PluginProfileResponse])
def revert_profile(profile_id: str, body: RevertProfileRequest,
                   svc: PluginProfileService = Depends(get_plugin_profile_service)):
    with _configuration_errors():
        profile = svc.revert_profile(profile_id, body.revision, expected_revision=body.expected_revision)
        return ApiResponse(data=profile.to_dict(), message="Profile restored as a new revision")
