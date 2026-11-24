"""
/api/v1/stimulation — Stimulation paradigm control endpoints.
"""
from fastapi import APIRouter, Depends

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
from omnibci.application.commands.stimulation_commands import (
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.ports.engine_port import STIMULATION_PEER

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
    with configuration_errors():
        result = await runtime.execute_current("stimulation", "start", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop stimulation",
)
def stop_stimulation(svc: StimulationAppService = Depends(get_stimulation_service)):
    with command_errors():
        svc.handle_stop_stimulation(StopStimulationCommand())
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
