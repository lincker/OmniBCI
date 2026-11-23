"""Plugin package installation, lifecycle, and version-profile routes."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.application.plugin_management.packages import (
    PluginPackageConflictError, PluginPackageError, PluginPackageNotFoundError, PluginPackageStorageError,
)
from omnibci.presentation.dependencies import get_configuration_service, get_plugin_package_service
from omnibci.presentation.errors import ApiError, configuration_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import PluginProfileResponse
from omnibci.presentation.schemas.plugin_packages import (
    CreateVersionProfileRequest, PackageInstallationResponse, PackageVersionResponse,
    ProfileVersionProposalResponse, ProfileVersionRequest, SetPackageEnabledRequest,
)

router = APIRouter(prefix="/plugin-packages", tags=["Plugin packages"])


@contextmanager
def package_errors():
    try:
        yield
    except PluginPackageNotFoundError as exc:
        raise ApiError(404, ErrorCode.NOT_FOUND, str(exc)) from exc
    except (PluginPackageConflictError, ProfileRevisionConflict) as exc:
        raise ApiError(409, ErrorCode.CONFLICT, str(exc)) from exc
    except PluginPackageStorageError as exc:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, str(exc)) from exc
    except PluginPackageError as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from exc


@router.get("", response_model=ApiResponse[list[PackageVersionResponse]])
def list_packages(svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), configuration_errors():
        return ApiResponse(data=[asdict(item) for item in svc.list()])


@router.post("/install", status_code=201, response_model=ApiResponse[PackageInstallationResponse],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/zip": {"schema": {"type": "string", "format": "binary"}},
                 "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
             }}})
async def install_package(request: Request, svc: PluginPackageService = Depends(get_plugin_package_service)):
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {"application/zip", "application/octet-stream"}:
        raise ApiError(415, ErrorCode.VALIDATION_ERROR, "Upload a raw ZIP with application/zip content type")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdigit()
                                 or len(declared) > 20 or int(declared) > svc.max_archive_bytes):
        raise ApiError(413, ErrorCode.VALIDATION_ERROR, "Plugin upload exceeds the size limit")
    with tempfile.TemporaryDirectory(prefix="omnibci-package-upload-") as directory:
        archive = Path(directory) / "package.zip"
        size = 0
        with archive.open("xb") as writer:
            async for chunk in request.stream():
                size += len(chunk)
                if size > svc.max_archive_bytes:
                    raise ApiError(413, ErrorCode.VALIDATION_ERROR, "Plugin upload exceeds the size limit")
                await run_in_threadpool(writer.write, chunk)
        if size == 0 or declared is not None and size != int(declared):
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, "Plugin upload is empty or truncated")
        with package_errors():
            installed = await run_in_threadpool(svc.install, archive)
            return ApiResponse(message="Package installed; enable it before selecting a profile", data=asdict(installed))


@router.put("/{package_id}/{version}/enabled", response_model=ApiResponse[PackageVersionResponse])
def set_enabled(package_id: str, version: str, body: SetPackageEnabledRequest,
                svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), configuration_errors():
        return ApiResponse(data=asdict(svc.set_enabled(package_id, version, body.enabled)))


@router.delete("/{package_id}/{version}", response_model=ApiResponse)
def remove(package_id: str, version: str, svc: PluginPackageService = Depends(get_plugin_package_service)):
    with package_errors(), configuration_errors():
        svc.remove(package_id, version)
        return ApiResponse(message="Package version removed")


@router.post("/profile-version-proposals", response_model=ApiResponse[ProfileVersionProposalResponse])
def propose_profile(body: ProfileVersionRequest, svc: ConfigurationService = Depends(get_configuration_service)):
    with package_errors(), configuration_errors():
        proposal = svc.propose_profile(**body.model_dump())
        data = asdict(proposal)
        if proposal.processing_input is not None:
            data["processing_input"] = proposal.processing_input.to_dict()
        return ApiResponse(data=data)


@router.post("/version-profiles", status_code=201, response_model=ApiResponse[PluginProfileResponse])
def create_version_profile(body: CreateVersionProfileRequest,
                           svc: ConfigurationService = Depends(get_configuration_service)):
    with package_errors(), configuration_errors():
        profile = svc.create_version_profile(**body.model_dump())
        return ApiResponse(message="New profile created; select and apply it explicitly", data=profile.to_dict())
