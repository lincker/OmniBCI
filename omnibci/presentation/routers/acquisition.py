"""
/api/v1/acquisition — Data acquisition control endpoints.
"""
from dataclasses import asdict
from fastapi import APIRouter, Depends, Path, Query
from starlette.concurrency import run_in_threadpool

from omnibci.presentation.dependencies import (
    get_acquisition_service, get_engine_service, get_runtime_configuration,
)
from omnibci.presentation.schemas.runtime_config import ModuleActionRequest, RuntimeOperationResponse
from omnibci.presentation.errors import (
    ApiError,
    COMMAND_EXECUTION_RESPONSES,
    command_errors,
    configuration_errors,
)
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.acquisition import (
    AcquisitionMetadataResponse, AcquisitionStatusResponse,
    AcquisitionPreviewResponse, AcquisitionPreviewChunkResponse,
    StartAcquisitionPreviewRequest,
    StartReadingRequest,
)
from omnibci.application.commands.acquisition_commands import (
    DisconnectDataProviderCommand,
    StopStreamCommand,
)
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.engine_service import NeuroEngineService

router = APIRouter(prefix="/acquisition", tags=["Acquisition"])


@router.post(
    "/connect",
    response_model=ApiResponse[RuntimeOperationResponse],
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Connect data source and wait for execution",
)
async def connect(body: ModuleActionRequest, runtime=Depends(get_runtime_configuration)):
    with configuration_errors():
        result = await runtime.execute_current("acquisition", "connect", **body.model_dump())
        return ApiResponse(data=result)


@router.post(
    "/disconnect",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Disconnect data source and wait for execution",
)
async def disconnect(svc: AcquisitionAppService = Depends(get_acquisition_service),
                     engine: NeuroEngineService = Depends(get_engine_service)):
    with command_errors():
        try:
            if engine.is_module_running("acquisition"):
                await run_in_threadpool(svc.handle_disconnect, DisconnectDataProviderCommand())
        finally:
            await engine.stop_module("acquisition")
            svc.reset()
        return ApiResponse(message="Data provider disconnected")


@router.post(
    "/stream/start",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Start EEG data stream and wait for execution",
)
async def start_stream(body: StartReadingRequest, engine: NeuroEngineService = Depends(get_engine_service)):
    with command_errors():
        await engine.start_reading(**body.model_dump())
        return ApiResponse(message="Stream started")


@router.post(
    "/stream/stop",
    response_model=ApiResponse,
    responses=COMMAND_EXECUTION_RESPONSES,
    summary="Stop EEG data stream and wait for execution",
)
def stop_stream(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    with command_errors():
        svc.handle_stop_stream(StopStreamCommand())
        return ApiResponse(message="Stream stopped")


@router.get(
    "/status",
    response_model=ApiResponse[AcquisitionStatusResponse],
    summary="Get acquisition status",
)
def get_status(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    result = svc.handle_get_status(GetAcquisitionStatusQuery())
    return ApiResponse(data=AcquisitionStatusResponse(**asdict(result)))


def _metadata_response(svc: AcquisitionAppService, *, refresh: bool = False):
    with command_errors():
        return ApiResponse(data=svc.get_metadata(refresh=refresh))


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
    with command_errors():
        try:
            return ApiResponse(data=callback(*args, **kwargs))
        except LookupError as exc:
            raise ApiError(404, ErrorCode.NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from exc


@router.post("/preview/start", response_model=ApiResponse[AcquisitionPreviewResponse],
             responses=COMMAND_EXECUTION_RESPONSES, summary="Capture a bounded or rolling selection of signal chunks")
def start_preview(body: StartAcquisitionPreviewRequest,
                  svc: AcquisitionAppService = Depends(get_acquisition_service)):
    return _preview_response(svc.start_preview, **body.model_dump())


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
