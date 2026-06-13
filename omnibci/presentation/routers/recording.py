"""
/api/v1/recording — one-call "record a run" orchestration.

Convenience over the primitives: start = create+start session then start stream;
stop = stop stream, drain, then end session. The primitive endpoints
(/sessions, /acquisition/stream/*) remain for advanced flows.
"""
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_recording_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.recording import (
    RecordingResponse,
    StartRecordingRequest,
)
from omnibci.application.services.recording_service import (
    RecordingService,
    RecordingConflictError,
    NoActiveRecordingError,
)

router = APIRouter(prefix="/recording", tags=["Recording"])


@router.post(
    "/start",
    response_model=ApiResponse[RecordingResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Start a recording (session + stream)",
)
def start_recording(
    body: StartRecordingRequest = StartRecordingRequest(),
    svc: RecordingService = Depends(get_recording_service),
):
    try:
        session_id = svc.start(paradigm=body.paradigm)
    except RecordingConflictError as e:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.ALREADY_RECORDING, str(e))
    except RuntimeError as e:
        # engine not ready (session rolled back inside the service)
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))
    return ApiResponse(
        message="Recording started",
        data=RecordingResponse(session_id=session_id),
    )


@router.post(
    "/stop",
    response_model=ApiResponse[RecordingResponse],
    summary="Stop the active recording (stream + session)",
)
def stop_recording(svc: RecordingService = Depends(get_recording_service)):
    try:
        session_id = svc.stop()
    except NoActiveRecordingError as e:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.NO_ACTIVE_RECORDING, str(e))
    return ApiResponse(
        message="Recording stopped",
        data=RecordingResponse(session_id=session_id),
    )
