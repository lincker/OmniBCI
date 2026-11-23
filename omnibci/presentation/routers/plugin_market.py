"""Only operator-configured trusted repositories are available to the console."""
from dataclasses import asdict

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from omnibci.application.plugin_management.market_service import PluginMarketService
from omnibci.presentation.dependencies import get_plugin_market_service
from omnibci.presentation.routers.plugin_packages import package_errors
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.plugin_market import InstallMarketPackageRequest, MarketPackageResponse
from omnibci.presentation.schemas.plugin_packages import PackageInstallationResponse

router = APIRouter(prefix="/market", tags=["Plugin market"])


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
