"""
/api/v1/stimulation — Stimulation paradigm control endpoints.
"""
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import (
    get_engine_service, get_runtime_configuration, get_stimulation_service,
)
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse
from omnibci.presentation.errors import (
    COMMAND_EXECUTION_RESPONSES,
    command_errors,
    configuration_errors,
)
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.stimulation import (
    StartTrialRequest,
    StimulationStatusResponse,
)
from omnibci.application.commands.stimulation_commands import StartStimTrialCommand
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.stimulation_service import StimulationAppService


router = APIRouter(prefix="/stimulation", tags=["Stimulation"])


@router.get(
    "/status",
    response_model=ApiResponse[StimulationStatusResponse],
    summary="Stimulation subsystem status",
)
def stimulation_status(
    engine_service: NeuroEngineService = Depends(get_engine_service),
    svc: StimulationAppService = Depends(get_stimulation_service),
):
    return ApiResponse(data=svc.status(engine_service.is_engine_ready))


@router.post(
    "/start",
    response_model=ApiResponse[RuntimeOperationResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Apply current stimulation and prepare or start it",
)
async def start_stimulation(
    body: ModuleActionRequest,
    runtime=Depends(get_runtime_configuration),
):
    with configuration_errors():
        result = await runtime.execute_current("stimulation", "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/prepare",
    response_model=ApiResponse,
    summary="Prepare stimulation for an external connection",
)
def prepare_stimulation(svc: StimulationAppService = Depends(get_stimulation_service)):
    with command_errors(runtime_status=status.HTTP_409_CONFLICT):
        svc.prepare_external()
        return ApiResponse(message="Stimulation ready for a connection")


@router.post(
    "/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop stimulation",
)
def stop_stimulation(svc: StimulationAppService = Depends(get_stimulation_service)):
    with command_errors():
        svc.stop()
        return ApiResponse(message="Stimulation stopped")


@router.post(
    "/trial/start",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Trigger a stimulation trial",
)
def start_trial(
    body: StartTrialRequest,
    svc: StimulationAppService = Depends(get_stimulation_service),
):
    with command_errors():
        svc.handle_start_trial(StartStimTrialCommand(
            n_trials=body.n_trials,
            trial_params=body.trial_params,
        ))
        return ApiResponse(message="Trial started")
