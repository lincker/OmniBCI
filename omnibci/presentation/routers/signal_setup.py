"""Edit saved channels and preview transformations without changing the run."""
from fastapi import APIRouter, Depends, Request

from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.signal_setup import (
    SaveSignalSetupRequest, PreviewSignalSetupRequest, SignalSetupResponse,
    SignalPreviewResponse, SignalTemplatesResponse, SignalTemplateResponse,
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


@router.put("", response_model=ApiResponse[SignalSetupResponse], response_model_exclude_none=True)
def save_signal_setup(body: SaveSignalSetupRequest, service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.save(body.setup.model_dump(exclude_none=True),
                                         expected_revision=body.expected_revision))


@router.post("/preview", response_model=ApiResponse[SignalPreviewResponse])
def preview_signal_setup(body: PreviewSignalSetupRequest, service=Depends(get_signal_setup_service)):
    return ApiResponse(data=service.preview(
        body.setup.model_dump(exclude_none=True),
        body.metadata.model_dump(exclude_none=True) if body.metadata is not None else None))


@router.get("/templates", response_model=ApiResponse[SignalTemplatesResponse])
def signal_templates(service=Depends(get_signal_setup_service)):
    return ApiResponse(data={"templates": service.layouts.names()})


@router.get("/templates/{name}", response_model=ApiResponse[SignalTemplateResponse])
def signal_template(name: str, service=Depends(get_signal_setup_service)):
    try:
        return ApiResponse(data=service.layouts.get(name))
    except ValueError as exc:
        raise ConfigProfileValidationError(str(exc)) from exc
