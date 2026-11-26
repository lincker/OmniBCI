"""
/api/v1/actuation — Actuation subsystem control endpoints.
"""
from fastapi import APIRouter, Depends, status

from omnibci.application.services.actuation_service import ActuationAppService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.presentation.dependencies import (
    get_actuation_service,
    get_engine_service,
    get_runtime_configuration,
)
from omnibci.presentation.errors import (
    COMMAND_EXECUTION_RESPONSES,
    command_errors,
    configuration_errors,
)
from omnibci.presentation.schemas.actuation import ActuationStatusResponse
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse

router = APIRouter(prefix="/actuation", tags=["Actuation"])


@router.get(
    "/status",
    response_model=ApiResponse[ActuationStatusResponse],
    summary="Actuation subsystem status",
)
def actuation_status(
    engine_service: NeuroEngineService = Depends(get_engine_service),
    svc: ActuationAppService = Depends(get_actuation_service),
):
    return ApiResponse(data=ActuationStatusResponse(
        **svc.status(engine_service.is_engine_ready),
    ))


@router.post(
    "/start",
    response_model=ApiResponse[RuntimeOperationResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Apply current actuation and prepare or start it",
)
async def start_actuation(
    body: ModuleActionRequest,
    runtime=Depends(get_runtime_configuration),
):
    with configuration_errors():
        result = await runtime.execute_current("actuation", "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/prepare",
    response_model=ApiResponse,
    summary="Prepare actuation for an external connection",
)
def prepare_actuation(svc: ActuationAppService = Depends(get_actuation_service)):
    with command_errors(runtime_status=status.HTTP_409_CONFLICT):
        svc.prepare_external()
        return ApiResponse(message="Actuation ready for a connection")


@router.post(
    "/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop actuation",
)
async def stop_actuation(svc: ActuationAppService = Depends(get_actuation_service)):
    with command_errors(runtime_status=status.HTTP_409_CONFLICT):
        await svc.stop()
        return ApiResponse(message="Actuation stopped")
