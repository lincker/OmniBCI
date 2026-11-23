"""Schema-driven profiles; saving never silently activates changed values."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from omnibci.application.configuration.definitions import MODULES, ENGINE_SETTING_DEFAULTS
from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.configuration.service import ConfigurationService
from omnibci.presentation.errors import ApiError, configuration_errors
from omnibci.presentation.dependencies import get_configuration_service
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import (
    ConfigurationModule, ModuleActivationResponse, ModulesResponse, EngineSettingsResponse,
    ModuleHistoryResponse, ProfileHistoryResponse, RevertProfileRequest, CreatePluginProfileRequest,
    PluginConfigurationResponse, PluginKind, PluginProfileResponse, UpdatePluginProfileRequest,
)

router = APIRouter(prefix="/config", tags=["Plugin configuration"])


@router.get("/plugin-catalog/{kind}", response_model=ApiResponse[list[PluginConfigurationResponse]])
def plugin_catalog(kind: PluginKind, svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=[asdict(item) for item in svc.list_plugin_configurations(kind)])


@router.get("/plugin-profiles", response_model=ApiResponse[list[PluginProfileResponse]])
def list_profiles(kind: PluginKind | None = None,
                  svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=[item.to_dict() for item in svc.list_profiles(kind)])


@router.post("/plugin-profiles", status_code=201, response_model=ApiResponse[PluginProfileResponse])
def create_profile(body: CreatePluginProfileRequest,
                   svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        values = {}
        try:
            selection = PluginSelection(**body.selection.model_dump())
            if body.processing_input is not None:
                values["processing_input"] = ProcessingInput.from_dict(body.processing_input.model_dump(exclude_none=True))
        except (ValueError, TypeError) as exc:
            raise PluginProfileValidationError(str(exc)) from exc
        profile = svc.create(body.kind, selection, body.name, body.parameters,
                             **values)
        return ApiResponse(message="Profile created", data=profile.to_dict())


@router.get("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def get_profile(profile_id: str, svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=svc.get_profile(profile_id).to_dict())


@router.put("/plugin-profiles/{profile_id}", response_model=ApiResponse[PluginProfileResponse])
def update_profile(profile_id: str, body: UpdatePluginProfileRequest,
                   svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        values = body.model_dump(exclude={"processing_input"})
        if "processing_input" in body.model_fields_set:
            try:
                values["processing_input"] = (
                    ProcessingInput.from_dict(body.processing_input.model_dump(exclude_none=True))
                    if body.processing_input is not None else None)
            except (ValueError, TypeError) as exc:
                raise PluginProfileValidationError(str(exc)) from exc
        profile = svc.update(profile_id, **values)
        return ApiResponse(message="Profile saved", data=profile.to_dict())


@router.delete("/plugin-profiles/{profile_id}", response_model=ApiResponse)
def delete_profile(profile_id: str, expected_revision: int = Query(ge=1),
                   svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        svc.delete(profile_id, expected_revision=expected_revision)
        return ApiResponse(message="Profile deleted")


def _module_response(module, activation, svc):
    return activation.to_dict() if activation is not None else {
        "module": module, "revision": None, "settings": svc.default_module_settings(module), "bindings": [],
    }


def _engine_response(engine_settings):
    return engine_settings.to_dict() if engine_settings is not None else {"revision": None, "settings": dict(ENGINE_SETTING_DEFAULTS)}


@router.get("/modules", response_model=ApiResponse[ModulesResponse])
def get_modules(svc: ConfigurationService = Depends(get_configuration_service)):
    modules, errors = {}, {}
    for module in MODULES:
        try:
            modules[module] = _module_response(module, svc.get_module(module), svc)
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            errors[module] = str(exc)
    try:
        engine_settings = _engine_response(svc.get_engine())
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        engine_settings = None
        errors["engine"] = str(exc)
    return ApiResponse(data={"modules": modules, "engine": engine_settings, "errors": errors})


@router.get("/modules/{module}", response_model=ApiResponse[ModuleActivationResponse])
def get_module(module: ConfigurationModule, svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=_module_response(module, svc.get_module(module), svc))


@router.get("/modules/{module}/history", response_model=ApiResponse[list[ModuleHistoryResponse]])
def module_history(module: ConfigurationModule, svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=[{
            "entry_id": entry.entry_id, "revision": entry.revision, "saved_at": entry.saved_at,
            "restored_from": entry.restored_from,
            "binding_summary": [f"{b.slot}: {b.selection.package_id}@{b.selection.version}/{b.selection.contribution_id}" for b in entry.activation.bindings],
        } for entry in svc.module_history(module)])


@router.get("/modules/{module}/history/{entry_id}", response_model=ApiResponse[ModuleActivationResponse])
def module_history_entry(module: ConfigurationModule, entry_id: str,
                         svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        entry = svc.module_history_entry(module, entry_id)
        if entry is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Module history entry not found")
        return ApiResponse(data=entry.activation.to_dict())


@router.get("/engine", response_model=ApiResponse[EngineSettingsResponse])
def get_engine(svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=_engine_response(svc.get_engine()))


@router.get("/plugin-profiles/{profile_id}/history", response_model=ApiResponse[list[ProfileHistoryResponse]])
def profile_history(profile_id: str, svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        return ApiResponse(data=[entry.to_dict() for entry in svc.profile_history(profile_id)])


@router.post("/plugin-profiles/{profile_id}/revert", response_model=ApiResponse[PluginProfileResponse])
def revert_profile(profile_id: str, body: RevertProfileRequest,
                   svc: ConfigurationService = Depends(get_configuration_service)):
    with configuration_errors():
        profile = svc.revert_profile(profile_id, body.revision, expected_revision=body.expected_revision)
        return ApiResponse(data=profile.to_dict(), message="Profile restored as a new revision")
