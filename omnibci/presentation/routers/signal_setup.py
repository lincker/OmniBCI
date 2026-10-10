"""Inspect signal readiness and preview decoder profile input without changing a run."""
from fastapi import APIRouter, Depends, Request

from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.signal_setup import (
    PreviewSignalSetupRequest, SignalSetupResponse, SignalPreviewResponse,
)

router = APIRouter(prefix="/config/signal", tags=["Signal setup"])


def get_signal_setup_service(request: Request):
    service = getattr(getattr(request.app.state, "container", None), "signal_setup_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Signal setup is unavailable")
    return service


@router.get("", response_model=ApiResponse[SignalSetupResponse], response_model_exclude_none=True)
def read_signal_setup(service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.read())


@router.post("/preview", response_model=ApiResponse[SignalPreviewResponse])
def preview_signal_setup(body: PreviewSignalSetupRequest, service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.preview_input(
        body.processing_input.model_dump(exclude_none=True),
        body.metadata.model_dump(exclude_none=True) if body.metadata is not None else None))
