"""
/api/v1/recording — one-call "record a run" orchestration.

Convenience over the primitives: start = create+start session then start stream;
stop = stop stream, drain, then end session. The primitive endpoints
(/sessions, /acquisition/stream/*) remain for advanced flows.
"""
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_recording_service
from omnibci.presentation.errors import (
    ApiError,
    COMMAND_EXECUTION_RESPONSES,
    command_error_to_api_error,
)
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
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)

router = APIRouter(prefix="/recording", tags=["Recording"])


@router.post(
    "/start",
    response_model=ApiResponse[RecordingResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Start a recording (session + stream)",
)
def start_recording(
    body: StartRecordingRequest = StartRecordingRequest(),
    svc: RecordingService = Depends(get_recording_service),
):
    try:
        session_id = svc.start(paradigm=body.paradigm, subject_id=body.subject_id)
    except RecordingConflictError as e:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.ALREADY_RECORDING, str(e))
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
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
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop the active recording (stream + session)",
)
def stop_recording(svc: RecordingService = Depends(get_recording_service)):
    try:
        session_id = svc.stop()
    except NoActiveRecordingError as e:
        raise ApiError(status.HTTP_409_CONFLICT, ErrorCode.NO_ACTIVE_RECORDING, str(e))
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    return ApiResponse(
        message="Recording stopped",
        data=RecordingResponse(session_id=session_id),
    )
