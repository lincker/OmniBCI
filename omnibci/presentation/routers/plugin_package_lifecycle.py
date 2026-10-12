"""Enablement does not modify current profile bindings or start plugin code."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request

from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.routers.plugin_packages import package_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import PluginProfileResponse
from omnibci.presentation.schemas.plugin_package_lifecycle import (
    CreateVersionProfileRequest, ProfileVersionProposalResponse, ProfileVersionRequest,
    SetPackageEnabledRequest,
)
from omnibci.presentation.schemas.plugin_packages import PackageVersionResponse

router = APIRouter(prefix="/plugin-packages", tags=["Plugin packages"])


def get_plugin_package_service(request: Request) -> PluginPackageService:
    service = getattr(request.app.state, "plugin_package_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin package management is unavailable")
    return service


@router.put("/{package_id}/{version}/enabled", response_model=ApiResponse[PackageVersionResponse])
def set_enabled(package_id: str, version: str, body: SetPackageEnabledRequest,
                svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), _configuration_errors():
        return ApiResponse(data=asdict(svc.set_enabled(package_id, version, body.enabled)))


@router.delete("/{package_id}/{version}", response_model=ApiResponse)
def remove(package_id: str, version: str, svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), _configuration_errors():
        svc.remove(package_id, version)
        return ApiResponse(message="Package version removed")


@router.post("/profile-version-proposals", response_model=ApiResponse[ProfileVersionProposalResponse])
def propose_profile(body: ProfileVersionRequest, svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), _configuration_errors():
        proposal = svc.propose_profile(**body.model_dump())
        data = asdict(proposal)
        for name in ("manual_montage", "processing_input"):
            if getattr(proposal, name) is not None:
                data[name] = getattr(proposal, name).to_dict()
        return ApiResponse(data=data)


@router.post("/version-profiles", status_code=201, response_model=ApiResponse[PluginProfileResponse])
def create_version_profile(body: CreateVersionProfileRequest,
                           svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), _configuration_errors():
        profile = svc.create_version_profile(**body.model_dump())
        return ApiResponse(message="New profile created; select and apply it explicitly", data=profile.to_dict())
