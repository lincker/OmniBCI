"""
/api/v1/engine — NeuroEngine lifecycle endpoints.
"""
from omnibci.application.configuration.errors import ProfileRevisionConflict
from fastapi import APIRouter, Depends, status
from typing import Literal

from omnibci.presentation.dependencies import get_engine_service, get_acquisition_service
from omnibci.presentation.routers.runtime_config import get_runtime_configuration
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse
from omnibci.presentation.errors import ApiError, COMMAND_EXECUTION_RESPONSES, command_error_to_api_error
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.engine import EngineStatusResponse
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import GetEngineStatusQuery
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import (
    CommandDeliveryError, CommandExecutionError, CommandRejectedError, CommandTimeoutError,
)

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


@router.post("/modules/{module}/start", response_model=ApiResponse[RuntimeOperationResponse],
             responses=COMMAND_EXECUTION_RESPONSES, summary="Start an independent engine module")
async def start_module(
    module: Literal["processing", "actuation"], body: ModuleActionRequest,
    runtime=Depends(get_runtime_configuration),
):
    with _configuration_errors():
        result = await runtime.execute_current(module, "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post("/modules/{module}/prepare", response_model=ApiResponse,
             summary="Prepare an output module for an external connection")
async def prepare_module(
    module: Literal["stimulation", "actuation"],
    svc: NeuroEngineService = Depends(get_engine_service),
):
    try:
        await svc.prepare_output(module)
        return ApiResponse(message=f"{module.title()} ready for a connection")
    except ProfileRevisionConflict:
        raise
    except RuntimeError as exc:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.ENGINE_NOT_READY, str(exc))


@router.post("/modules/{module}/stop", response_model=ApiResponse,
             responses=COMMAND_EXECUTION_RESPONSES, summary="Stop an independent engine module")
async def stop_module(
    module: Literal["acquisition", "processing", "actuation"],
    svc: NeuroEngineService = Depends(get_engine_service),
    acq: AcquisitionAppService = Depends(get_acquisition_service),
):
    try:
        await svc.stop_module(module)
        if module == "acquisition":
            acq.reset()
        return ApiResponse(message=f"{module.title()} stopped")
    except (CommandDeliveryError, CommandRejectedError, CommandExecutionError, CommandTimeoutError) as exc:
        raise command_error_to_api_error(exc)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as exc:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.ENGINE_NOT_READY, str(exc))


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
            actuation_alive=result.actuation_alive,
            actuation_connected=result.actuation_connected,
        )
    )
