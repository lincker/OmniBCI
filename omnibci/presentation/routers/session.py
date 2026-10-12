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
import csv
import io
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import List, Literal

from omnibci.application.plugin_profiles import ProfileRevisionConflict
from fastapi import APIRouter, Depends, Query, Response, status

from omnibci.presentation.dependencies import get_session_service
from omnibci.presentation.errors import (
    ApiError,
    COMMAND_EXECUTION_RESPONSES,
    command_error_to_api_error,
)
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.session import (
    CreateSessionRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
    SessionExportResponse,
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
from omnibci.presentation.routers.system import APP_VERSION
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)

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
        config_revision=t.config_revision,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=ApiResponse[SessionResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new BCI session",
)
def create_session(
    body: CreateSessionRequest = CreateSessionRequest(),
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        session_id = svc.handle_create_session(CreateSessionCommand(paradigm=body.paradigm))
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as exc:
        raise command_error_to_api_error(exc)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorCode.ENGINE_NOT_READY,
            str(exc),
        )
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
            continuation_of=dto.continuation_of,
            config_revision=dto.config_revision,
            interruption_reason=dto.interruption_reason,
            signal_plan=dto.signal_plan,
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
            continuation_of=d.continuation_of,
            config_revision=d.config_revision,
            interruption_reason=d.interruption_reason,
            signal_plan=d.signal_plan,
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
            continuation_of=dto.continuation_of,
            config_revision=dto.config_revision,
            interruption_reason=dto.interruption_reason,
            signal_plan=dto.signal_plan,
        )
    )


@router.get("/export", response_model=SessionExportResponse, responses={
    200: {"content": {"text/csv": {"schema": {"type": "string"}}}},
}, summary="Export a consistent snapshot of selected sessions")
def export_sessions(
    session_id: List[str] | None = Query(default=None),
    format: Literal["json", "csv"] = "json",
    svc: SessionAppService = Depends(get_session_service),
):
    try:
        sessions = [SessionDetailResponse(**asdict(dto)) for dto in svc.export_snapshot(session_id)]
    except KeyError as exc:
        raise ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, str(exc))
    snapshot = SessionExportResponse(
        exported_at=datetime.now(timezone.utc), app_version=APP_VERSION, sessions=sessions,
    )
    headers = {
        "Content-Disposition": f'attachment; filename="omnibci-sessions.{format}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if format == "json":
        return Response(snapshot.model_dump_json(), media_type="application/json", headers=headers)
    output = io.StringIO(newline="")
    session_fields = list(SessionResponse.model_fields)
    trial_fields = list(TrialResponse.model_fields)
    fields = ["format_version", "exported_at", "app_version"] + [
        "session_config_revision" if name == "config_revision" else name for name in session_fields
    ] + ["trial_config_revision" if name == "config_revision" else name for name in trial_fields]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for session in sessions:
        metadata = session.model_dump(mode="json", exclude={"trials"})
        if metadata.get("signal_plan") is not None:
            metadata["signal_plan"] = json.dumps(metadata["signal_plan"], ensure_ascii=False, allow_nan=False)
        metadata["session_config_revision"] = metadata.pop("config_revision")
        for trial in session.trials or [None]:
            row = {"format_version": 1, "exported_at": snapshot.exported_at.isoformat(),
                   "app_version": APP_VERSION, **metadata}
            if trial is not None:
                values = trial.model_dump(mode="json")
                values["trial_config_revision"] = values.pop("config_revision")
                row.update(values)
            # Spreadsheet applications otherwise interpret user labels as formulas.
            writer.writerow({key: "'" + value if isinstance(value, str)
                             and value.lstrip().startswith(("=", "+", "-", "@")) else value
                             for key, value in row.items()})
    return Response(output.getvalue(), media_type="text/csv", headers=headers)


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
            continuation_of=dto.continuation_of,
            config_revision=dto.config_revision,
            interruption_reason=dto.interruption_reason,
            signal_plan=dto.signal_plan,
        )
    )


@router.delete(
    "/{session_id}",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
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
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as exc:
        raise command_error_to_api_error(exc)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorCode.ENGINE_NOT_READY,
            str(exc),
        )
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
