"""
/api/v1/stimulation — Stimulation paradigm control endpoints.
"""
from fastapi import APIRouter, Depends, Request, status

from omnibci.presentation.dependencies import get_stimulation_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.stimulation import (
    ParadigmsResponse,
    ParadigmInfo,
    StartStimulationRequest,
    StartTrialRequest,
    StimulationStatusResponse,
)
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.infrastructure.plugins.plugin_catalog import list_plugins
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


@router.get(
    "/paradigms",
    response_model=ApiResponse[ParadigmsResponse],
    summary="List available stimulation paradigms",
)
def list_paradigms():
    # Dual-root manifest-based discovery (pure filesystem + TOML, no OpenGL import).
    paradigms = [ParadigmInfo.from_plugin_info(p) for p in list_plugins("paradigm")]
    return ApiResponse(data=ParadigmsResponse(paradigms=paradigms))


@router.post("/start", response_model=ApiResponse, summary="Start stimulation")
def start_stimulation(
    body: StartStimulationRequest = StartStimulationRequest(),
    svc: StimulationAppService = Depends(get_stimulation_service),
):
    try:
        svc.handle_start_stimulation(StartStimulationCommand(paradigm=body.paradigm))
        return ApiResponse(message="Stimulation started")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/stop", response_model=ApiResponse, summary="Stop stimulation")
def stop_stimulation(svc: StimulationAppService = Depends(get_stimulation_service)):
    try:
        svc.handle_stop_stimulation(StopStimulationCommand())
        return ApiResponse(message="Stimulation stopped")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/trial/start", response_model=ApiResponse, summary="Trigger a stimulation trial")
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
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))
