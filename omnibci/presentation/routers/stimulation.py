"""
/api/v1/stimulation — Stimulation paradigm control endpoints.
"""
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_stimulation_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.stimulation import (
    ParadigmsResponse,
    ParadigmInfo,
    StartStimulationRequest,
    StartTrialRequest,
)
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.services.stimulation_service import StimulationAppService

router = APIRouter(prefix="/stimulation", tags=["Stimulation"])


@router.get(
    "/paradigms",
    response_model=ApiResponse[ParadigmsResponse],
    summary="List available stimulation paradigms",
)
def list_paradigms():
    # Dual-root manifest-based discovery (no OpenGL import).
    from omnibci.infrastructure.stimulation.paradigm_discovery import list_paradigms as _discover  # noqa: PLC0415
    paradigms = _discover()
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
        svc.handle_start_trial(StartStimTrialCommand(target_index=body.target_index))
        return ApiResponse(message="Trial started")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))
