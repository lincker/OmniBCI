"""Inspect signal readiness and preview decoder profile input without changing a run."""
from fastapi import APIRouter, Depends

from omnibci.presentation.dependencies import get_signal_setup_service
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.signal_setup import (
    PreviewSignalSetupRequest, SignalSetupResponse, SignalPreviewResponse,
)

router = APIRouter(prefix="/config/signal", tags=["Signal setup"])


@router.get("", response_model=ApiResponse[SignalSetupResponse], response_model_exclude_none=True)
def read_signal_setup(service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.read())


@router.post("/preview", response_model=ApiResponse[SignalPreviewResponse])
def preview_signal_setup(body: PreviewSignalSetupRequest, service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.preview_input(
        body.processing_input.model_dump(exclude_none=True),
        expected_source_revision=body.expected_source_revision,
        decoder_selection=body.decoder_selection.model_dump() if body.decoder_selection else None))
