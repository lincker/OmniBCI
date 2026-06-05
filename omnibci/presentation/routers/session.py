"""
/api/v1/sessions — BCI session lifecycle endpoints.

POST   /sessions              Create + start a new session
GET    /sessions              List all sessions
GET    /sessions/active       Get the currently active session
GET    /sessions/{id}         Get session metadata
DELETE /sessions/{id}         End a session
GET    /sessions/{id}/trials  Full trial history
GET    /sessions/{id}/trials/last  Most recent classification result
"""
from typing import List

from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_session_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.session import (
    CreateSessionRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
    TrialResponse,
)
from omnibci.application.commands.session_commands import (
    CreateSessionCommand,
    EndSessionCommand,
)
from omnibci.application.queries.session_queries import (
    GetLastClassificationQuery,
    GetSessionQuery,
    GetSessionTrialsQuery,
    ListSessionsQuery,
    TrialDTO,
)
from omnibci.application.services.session_service import SessionAppService

router = APIRouter(prefix="/sessions", tags=["Session"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trial_dto_to_response(t: TrialDTO) -> TrialResponse:
    return TrialResponse(
        trial_number=t.trial_number,
        target_id=t.target_id,
        event_id=t.event_id,
        confidence=t.confidence,
        forced=t.forced,
        produced_at=t.produced_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=ApiResponse[SessionResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new BCI session",
)
def create_session(
    body: CreateSessionRequest = CreateSessionRequest(),
    svc: SessionAppService = Depends(get_session_service),
):
    session_id = svc.handle_create_session(CreateSessionCommand(paradigm=body.paradigm))
    dto = svc.handle_get_session(GetSessionQuery(session_id))
    return ApiResponse(
        message="Session created",
        data=SessionResponse(
            session_id=dto.session_id,
            paradigm=dto.paradigm,
            state=dto.state,
            trial_count=dto.trial_count,
            started_at=dto.started_at,
            ended_at=dto.ended_at,
        ),
    )


@router.get(
    "",
    response_model=ApiResponse[SessionListResponse],
    summary="List all sessions",
)
def list_sessions(svc: SessionAppService = Depends(get_session_service)):
    dtos = svc.handle_list_sessions(ListSessionsQuery())
    sessions = [
        SessionResponse(
            session_id=d.session_id,
            paradigm=d.paradigm,
            state=d.state,
            trial_count=d.trial_count,
            started_at=d.started_at,
            ended_at=d.ended_at,
        )
        for d in dtos
    ]
    return ApiResponse(data=SessionListResponse(sessions=sessions, total=len(sessions)))


@router.get(
    "/active",
    response_model=ApiResponse[SessionResponse],
    summary="Get the currently active session",
)
def get_active_session(svc: SessionAppService = Depends(get_session_service)):
    session_id = svc.active_session_id
    if session_id is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.NO_ACTIVE_SESSION, "No active session")
    dto = svc.handle_get_session(GetSessionQuery(session_id))
    return ApiResponse(
        data=SessionResponse(
            session_id=dto.session_id,
            paradigm=dto.paradigm,
            state=dto.state,
            trial_count=dto.trial_count,
            started_at=dto.started_at,
            ended_at=dto.ended_at,
        )
    )


@router.get(
    "/{session_id}",
    response_model=ApiResponse[SessionResponse],
    summary="Get session metadata",
)
def get_session(
    session_id: str,
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        dto = svc.handle_get_session(GetSessionQuery(session_id))
    except KeyError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, f"Session '{session_id}' not found")
    return ApiResponse(
        data=SessionResponse(
            session_id=dto.session_id,
            paradigm=dto.paradigm,
            state=dto.state,
            trial_count=dto.trial_count,
            started_at=dto.started_at,
            ended_at=dto.ended_at,
        )
    )


@router.delete(
    "/{session_id}",
    response_model=ApiResponse,
    summary="End a session",
)
def end_session(
    session_id: str,
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        svc.handle_end_session(EndSessionCommand(session_id=session_id))
    except KeyError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, f"Session '{session_id}' not found")
    return ApiResponse(message="Session ended")


@router.get(
    "/{session_id}/trials",
    response_model=ApiResponse[List[TrialResponse]],
    summary="Get full trial history for a session",
)
def get_trials(
    session_id: str,
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        trials = svc.handle_get_trials(GetSessionTrialsQuery(session_id))
    except KeyError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, f"Session '{session_id}' not found")
    return ApiResponse(data=[_trial_dto_to_response(t) for t in trials])


@router.get(
    "/{session_id}/trials/last",
    response_model=ApiResponse[TrialResponse],
    summary="Get the most recent classification result",
)
def get_last_trial(
    session_id: str,
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        trial = svc.handle_get_last_classification(GetLastClassificationQuery(session_id))
    except KeyError:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, f"Session '{session_id}' not found")
    if trial is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.NO_TRIALS, "No trials recorded yet")
    return ApiResponse(data=_trial_dto_to_response(trial))
