"""
/api/v1/engine — NeuroEngine foundation lifecycle endpoints.
"""
from dataclasses import asdict
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_acquisition_service, get_engine_service
from omnibci.presentation.errors import command_errors
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.engine import EngineStatusResponse
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import GetEngineStatusQuery
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService

router = APIRouter(prefix="/engine", tags=["Engine"])


@router.post("/start", response_model=ApiResponse, summary="Start NeuroEngine")
async def start_engine(svc: NeuroEngineService = Depends(get_engine_service)):
    with command_errors(runtime_status=status.HTTP_409_CONFLICT):
        await svc.handle_start_engine(StartEngineCommand())
        return ApiResponse(message="NeuroEngine started")


@router.post("/stop", response_model=ApiResponse, summary="Stop NeuroEngine")
async def stop_engine(
    svc: NeuroEngineService = Depends(get_engine_service),
    acq: AcquisitionAppService = Depends(get_acquisition_service),
):
    await svc.handle_stop_engine(StopEngineCommand())
    # Stopping the engine kills the acquisition worker -> clear the (now stale)
    # connected/streaming flags so the acquisition panel matches reality.
    acq.reset()
    return ApiResponse(message="NeuroEngine stopped")


@router.get(
    "/status",
    response_model=ApiResponse[EngineStatusResponse],
    summary="Get NeuroEngine status",
)
def get_status(svc: NeuroEngineService = Depends(get_engine_service)):
    result = svc.handle_get_status(GetEngineStatusQuery())
    return ApiResponse(data=EngineStatusResponse(**asdict(result)))
