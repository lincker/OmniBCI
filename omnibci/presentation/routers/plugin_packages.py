"""Bounded raw ZIP upload avoids buffering uploads or requiring multipart parsing."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from omnibci.application.plugin_packages import (
    PluginPackageConflictError, PluginPackageError, PluginPackageNotFoundError, PluginPackageStorageError,
)
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.plugin_installation_service import PluginInstallationService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_packages import PackageInstallationResponse, PackageVersionResponse

router = APIRouter(prefix="/plugin-packages", tags=["Plugin packages"])


def get_plugin_installation_service(request: Request) -> PluginInstallationService:
    service = getattr(request.app.state, "plugin_installation_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin installation is unavailable")
    return service


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
def list_packages(svc: PluginInstallationService = Depends(get_plugin_installation_service)):
    with package_errors(), _configuration_errors():
        return ApiResponse(data=[asdict(item) for item in svc.list()])


@router.post("/install", status_code=201, response_model=ApiResponse[PackageInstallationResponse],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/zip": {"schema": {"type": "string", "format": "binary"}},
                 "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
             }}})
async def install_package(request: Request, svc: PluginInstallationService = Depends(get_plugin_installation_service)):
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
