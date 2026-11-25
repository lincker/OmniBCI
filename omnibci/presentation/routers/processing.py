"""
/api/v1/processing — Signal processing subsystem control endpoints.
"""
from fastapi import APIRouter, Depends, status

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.presentation.dependencies import get_engine_service, get_runtime_configuration
from omnibci.presentation.errors import COMMAND_EXECUTION_RESPONSES, command_errors, configuration_errors
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse

router = APIRouter(prefix="/processing", tags=["Processing"])


@router.post(
    "/start",
    response_model=ApiResponse[RuntimeOperationResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Apply current signal processing configuration and start it",
)
async def start_processing(
    body: ModuleActionRequest,
    runtime=Depends(get_runtime_configuration),
):
    with configuration_errors():
        result = await runtime.execute_current("processing", "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop signal processing",
)
async def stop_processing(svc: NeuroEngineService = Depends(get_engine_service)):
    with command_errors(runtime_status=status.HTTP_409_CONFLICT):
        await svc.stop_module("processing")
        return ApiResponse(message="Processing stopped")
