"""
/api/v1/stimulation — Stimulation paradigm control endpoints.
"""
from omnibci.application.configuration.errors import ProfileRevisionConflict
from fastapi import APIRouter, Depends, Request, status

from omnibci.presentation.dependencies import get_stimulation_service
from omnibci.presentation.routers.runtime_config import get_runtime_configuration
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse
from omnibci.presentation.errors import (
    ApiError,
    COMMAND_EXECUTION_RESPONSES,
    command_error_to_api_error,
)
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.stimulation import (
    StartTrialRequest,
    StimulationStatusResponse,
)
from omnibci.application.commands.stimulation_commands import (
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.command_result_service import (
    CommandDeliveryError,
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)
from omnibci.application.ports.engine_port import STIMULATION_PEER

router = APIRouter(prefix="/stimulation", tags=["Stimulation"])


@router.get(
    "/status",
    response_model=ApiResponse[StimulationStatusResponse],
    summary="Stimulation subsystem status",
)
def stimulation_status(
    request: Request,
    svc: StimulationAppService = Depends(get_stimulation_service),
):
    engine_service = request.app.state.engine_service
    return ApiResponse(data=StimulationStatusResponse(
        active=svc.is_active,
        process_alive=svc.process_alive,
        engine_connected=engine_service.is_peer_connected(STIMULATION_PEER),
        paradigm=svc.current_paradigm,
    ))


@router.post("/start", response_model=ApiResponse[RuntimeOperationResponse], summary="Apply current stimulation and prepare or start it")
async def start_stimulation(
    body: ModuleActionRequest,
    runtime=Depends(get_runtime_configuration),
):
    with _configuration_errors():
        result = await runtime.execute_current("stimulation", "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop stimulation",
)
def stop_stimulation(svc: StimulationAppService = Depends(get_stimulation_service)):
    try:
        svc.handle_stop_stimulation(StopStimulationCommand())
        return ApiResponse(message="Stimulation stopped")
    except (CommandDeliveryError, CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post(
    "/trial/start",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Trigger a stimulation trial",
)
def start_trial(
    body: StartTrialRequest = StartTrialRequest(),
    svc: StimulationAppService = Depends(get_stimulation_service),
):
    try:
        svc.handle_start_trial(StartStimTrialCommand(
            n_trials=body.n_trials,
            trial_params=body.trial_params,
        ))
        return ApiResponse(message="Trial started")
    except (CommandDeliveryError, CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))
