"""
/api/v1/acquisition — Data acquisition control endpoints.
"""
from fastapi import APIRouter, Depends, status

from omnibci.presentation.dependencies import get_acquisition_service
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.acquisition import AcquisitionStatusResponse
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.services.acquisition_service import AcquisitionAppService

router = APIRouter(prefix="/acquisition", tags=["Acquisition"])


@router.post("/connect", response_model=ApiResponse, summary="Connect to data source")
def connect(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_connect(ConnectDataProviderCommand())
        return ApiResponse(message="Data provider connected")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/disconnect", response_model=ApiResponse, summary="Disconnect from data source")
def disconnect(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_disconnect(DisconnectDataProviderCommand())
        return ApiResponse(message="Data provider disconnected")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/stream/start", response_model=ApiResponse, summary="Start EEG data stream")
def start_stream(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_start_stream(StartStreamCommand())
        return ApiResponse(message="Stream started")
    except RuntimeError as e:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.ENGINE_NOT_READY, str(e))


@router.post("/stream/stop", response_model=ApiResponse, summary="Stop EEG data stream")
def stop_stream(svc: AcquisitionAppService = Depends(get_acquisition_service)):
    try:
        svc.handle_stop_stream(StopStreamCommand())
        return ApiResponse(message="Stream stopped")
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
        )
    )
