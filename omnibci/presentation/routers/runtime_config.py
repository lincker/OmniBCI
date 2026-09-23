"""Runtime configuration planning; no profile save implicitly applies changes."""
from fastapi import APIRouter, Depends, Request

from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.runtime_config import (
    RuntimeConfigurationResponse, RuntimePlanRequest, RuntimePlanResponse,
)

router = APIRouter(prefix="/config", tags=["Runtime configuration"])


def get_runtime_configuration(request: Request) -> RuntimeConfigurationService:
    service = getattr(getattr(request.app.state, "container", None), "runtime_configuration", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Runtime configuration is unavailable")
    return service


@router.get("/runtime", response_model=ApiResponse[RuntimeConfigurationResponse])
def runtime_status(service=Depends(get_runtime_configuration)):
    with _configuration_errors():
        return ApiResponse(data=service.status())


@router.post("/apply-plan", response_model=ApiResponse[RuntimePlanResponse])
def plan_configuration(body: RuntimePlanRequest, service=Depends(get_runtime_configuration)):
    with _configuration_errors():
        plan = service.plan(**body.model_dump())
        return ApiResponse(data=plan.to_dict())
