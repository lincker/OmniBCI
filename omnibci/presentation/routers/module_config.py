"""CRUD and activation for local module profiles."""
from dataclasses import asdict

from fastapi import APIRouter, Depends

from omnibci.application.services.config_service import ConfigService
from omnibci.presentation.dependencies import get_config_service
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.module_config import (
    CreateModuleProfileRequest, UpdateModuleProfileRequest, SelectModuleProfileRequest,
    ModuleProfileResponse, ModuleProfileListResponse, ModuleConfigResponse,
)

router = APIRouter(prefix="/config/modules/{module}", tags=["Module config"])


@router.get("", response_model=ApiResponse[ModuleConfigResponse])
def get_module_config(module: str, svc: ConfigService = Depends(get_config_service)):
    return ApiResponse(data=svc.module_detail(module))


@router.get("/profiles", response_model=ApiResponse[ModuleProfileListResponse])
def list_module_profiles(module: str, plugin_type: str | None = None,
                         svc: ConfigService = Depends(get_config_service)):
    return ApiResponse(data={"profiles": [asdict(p) for p in svc.list_module_profiles(module, plugin_type)]})


@router.post("/profiles", status_code=201, response_model=ApiResponse[ModuleProfileResponse])
def create_module_profile(module: str, body: CreateModuleProfileRequest,
                          svc: ConfigService = Depends(get_config_service)):
    profile = svc.create_module_profile(module, body.plugin_type, body.name, body.parameters)
    return ApiResponse(message="Profile created", data=asdict(profile))


@router.get("/profiles/{profile_id}", response_model=ApiResponse[ModuleProfileResponse])
def get_module_profile(module: str, profile_id: str,
                       svc: ConfigService = Depends(get_config_service)):
    return ApiResponse(data=asdict(svc.module_profile(module, profile_id)))


@router.put("/profiles/{profile_id}", response_model=ApiResponse[ModuleProfileResponse])
def update_module_profile(module: str, profile_id: str, body: UpdateModuleProfileRequest,
                          svc: ConfigService = Depends(get_config_service)):
    profile = svc.update_module_profile(module, profile_id, body.name, body.parameters)
    return ApiResponse(message="Profile saved", data=asdict(profile))


@router.delete("/profiles/{profile_id}", response_model=ApiResponse)
def delete_module_profile(module: str, profile_id: str,
                          svc: ConfigService = Depends(get_config_service)):
    svc.delete_module_profile(module, profile_id)
    return ApiResponse(message="Profile deleted")


@router.put("/active-profile", response_model=ApiResponse[ModuleConfigResponse])
def select_module_profile(module: str, body: SelectModuleProfileRequest,
                          svc: ConfigService = Depends(get_config_service)):
    svc.select_module_profile(module, body.profile_id)
    return ApiResponse(message="Profile selection saved", data=svc.module_detail(module))
