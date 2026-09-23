"""Only operator-configured trusted repositories are available to the console."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from omnibci.application.services.plugin_market_service import PluginMarketService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.routers.plugin_packages import package_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_market import InstallMarketPackageRequest, MarketPackageResponse
from omnibci.presentation.schemas.plugin_packages import PackageInstallationResponse

router = APIRouter(prefix="/market", tags=["Plugin market"])


def get_plugin_market_service(request: Request) -> PluginMarketService:
    service = getattr(request.app.state, "plugin_market_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin market is not configured")
    return service


@router.get("/catalog", response_model=ApiResponse[list[MarketPackageResponse]])
async def catalog(svc: PluginMarketService = Depends(get_plugin_market_service)):
    with package_errors():
        packages = await run_in_threadpool(svc.list)
        return ApiResponse(data=[asdict(item) for item in packages])


@router.post("/install", status_code=201, response_model=ApiResponse[PackageInstallationResponse])
async def install(body: InstallMarketPackageRequest, svc: PluginMarketService = Depends(get_plugin_market_service)):
    with package_errors():
        result = await run_in_threadpool(svc.install, body.package_id, body.version)
        return ApiResponse(message="Package installed; enable and select its profile explicitly", data=asdict(result))
