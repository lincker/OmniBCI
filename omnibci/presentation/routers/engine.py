"""
/api/v1/engine — NeuroEngine lifecycle endpoints.
"""
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_engine_service, get_acquisition_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
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
    try:
        await svc.handle_start_engine(StartEngineCommand())
        return ApiResponse(message="NeuroEngine started")
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/stop", response_model=ApiResponse, summary="Stop NeuroEngine")
async def stop_engine(
    svc: NeuroEngineService = Depends(get_engine_service),
    acq: AcquisitionAppService = Depends(get_acquisition_service),
):
    await svc.handle_stop_engine(StopEngineCommand())
    # Stopping the engine kills the acquisition worker → clear the (now stale)
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
    return ApiResponse(
        data=EngineStatusResponse(
            running=result.running,
            acquisition_alive=result.acquisition_alive,
            processor_alive=result.processor_alive,
            stimulation_connected=result.stimulation_connected,
        )
    )
