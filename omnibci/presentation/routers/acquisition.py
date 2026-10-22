"""
/api/v1/acquisition — Data acquisition control endpoints.
"""
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from fastapi import APIRouter, Depends, Path, Query, status

from omnibci.presentation.dependencies import get_acquisition_service
from omnibci.presentation.errors import (
    ApiError,
    COMMAND_EXECUTION_RESPONSES,
    command_error_to_api_error,
)
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.acquisition import (
    AcquisitionMetadataResponse, AcquisitionStatusResponse,
    AcquisitionPreviewResponse, AcquisitionPreviewChunkResponse,
    StartAcquisitionPreviewRequest, StopAcquisitionPreviewRequest,
)
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)

router = APIRouter(prefix="/acquisition", tags=["Acquisition"])


@router.post(
    "/connect",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Connect data source and wait for execution",
)
def connect(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_connect(ConnectDataProviderCommand())
        return ApiResponse(message="Data provider connected")
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post(
    "/disconnect",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Disconnect data source and wait for execution",
)
def disconnect(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_disconnect(DisconnectDataProviderCommand())
        return ApiResponse(message="Data provider disconnected")
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post(
    "/stream/start",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Start EEG data stream and wait for execution",
)
def start_stream(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_start_stream(StartStreamCommand())
        return ApiResponse(message="Stream started")
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post(
    "/stream/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop EEG data stream and wait for execution",
)
def stop_stream(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_stop_stream(StopStreamCommand())
        return ApiResponse(message="Stream stopped")
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.get(
    "/status",
    response_model=ApiResponse[AcquisitionStatusResponse],
    summary="Get acquisition status",
)
def get_status(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    result = svc.handle_get_status(GetAcquisitionStatusQuery())
    return ApiResponse(
        data=AcquisitionStatusResponse(
            connected=result.connected,
            streaming=result.streaming,
            source=result.source,
            sample_rate=result.sample_rate,
            channel_count=result.channel_count,
            metadata_available=result.metadata_available,
            metadata_provenance=result.metadata_provenance,
            connection_id=result.connection_id,
        )
    )


def _metadata_response(svc: AcquisitionAppService, *, refresh: bool = False):
    try:
        return ApiResponse(data=svc.get_metadata(refresh=refresh))
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as e:
        raise command_error_to_api_error(e)
    except ProfileRevisionConflict:
        raise
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.get(
    "/metadata", response_model=ApiResponse[AcquisitionMetadataResponse], responses=COMMAND_EXECUTION_RESPONSES,
    summary="Read metadata for the current source connection",
)
def get_metadata(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _metadata_response(svc)


@router.post(
    "/metadata/refresh", response_model=ApiResponse[AcquisitionMetadataResponse], responses=COMMAND_EXECUTION_RESPONSES,
    summary="Refresh metadata while the source is stopped",
)
def refresh_metadata(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _metadata_response(svc, refresh=True)


def _preview_response(callback, *args, **kwargs):
    try:
        return ApiResponse(data=callback(*args, **kwargs))
    except (CommandRejectedError, CommandExecutionError, CommandTimeoutError) as exc:
        raise command_error_to_api_error(exc)
    except ProfileRevisionConflict:
        raise
    except LookupError as exc:
        raise ApiError(404, ErrorCode.NOT_FOUND, str(exc))
    except ValueError as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc))
    except RuntimeError as exc:
        raise ApiError(503, ErrorCode.ENGINE_NOT_READY, str(exc))


@router.post("/preview/start", response_model=ApiResponse[AcquisitionPreviewResponse],
             responses=COMMAND_EXECUTION_RESPONSES, summary="Capture a bounded or rolling selection of signal chunks")
def start_preview(body: StartAcquisitionPreviewRequest,
                  svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _preview_response(svc.start_preview, **body.model_dump())


@router.post("/preview/stop", response_model=ApiResponse[AcquisitionPreviewResponse],
             responses=COMMAND_EXECUTION_RESPONSES, summary="Stop the preview capture and retain captured chunks")
def stop_preview(body: StopAcquisitionPreviewRequest,
                 svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _preview_response(svc.stop_preview, **body.model_dump())


@router.get("/preview", response_model=ApiResponse[AcquisitionPreviewResponse],
            responses=COMMAND_EXECUTION_RESPONSES, summary="Inspect preview capture and delivery status")
def get_preview(generation: str = Query(min_length=1, max_length=128),
                capture_id: str | None = Query(default=None, min_length=1, max_length=128),
                svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _preview_response(svc.get_preview, generation=generation, capture_id=capture_id)


@router.get("/preview/chunks/{sequence_id}", response_model=ApiResponse[AcquisitionPreviewChunkResponse],
            responses=COMMAND_EXECUTION_RESPONSES, summary="Read the exact retained rows and samples of a preview chunk")
def get_preview_chunk(sequence_id: int = Path(ge=0),
                      generation: str = Query(min_length=1, max_length=128),
                      capture_id: str = Query(min_length=1, max_length=128),
                      svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _preview_response(svc.get_preview_chunk, sequence_id, generation=generation, capture_id=capture_id)
