"""Runtime configuration planning; no profile save implicitly applies changes."""
from typing import Literal
from fastapi import APIRouter, Depends, Request

from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.routers.plugin_config import _configuration_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.runtime_config import (
    RuntimeConfigurationResponse, RuntimePlanRequest, RuntimePlanResponse,
    RuntimeApplyRequest, RuntimeOperationResponse,
    NodeConfigurationPublicationResponse,
)

router = APIRouter(prefix="/config", tags=["Runtime configuration"])


def get_runtime_configuration(request: Request) -> RuntimeConfigurationService:
    service = getattr(getattr(request.app.state, "container", None), "runtime_configuration", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Runtime configuration is unavailable")
    return service


@router.get("/nodes/{role}", response_model=ApiResponse[NodeConfigurationPublicationResponse])
def node_configuration(role: Literal["stimulation", "actuation"], request: Request):
    service = getattr(getattr(request.app.state, "container", None), "node_configuration", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Node configuration is unavailable")
    with _configuration_errors():
        return ApiResponse(data=service.configuration(role))


@router.get("/runtime", response_model=ApiResponse[RuntimeConfigurationResponse])
def runtime_status(service=Depends(get_runtime_configuration)):
    with _configuration_errors():
        return ApiResponse(data=service.status())


@router.post("/apply-plan", response_model=ApiResponse[RuntimePlanResponse])
def plan_configuration(body: RuntimePlanRequest, service=Depends(get_runtime_configuration)):
    with _configuration_errors():
        plan = service.plan(**body.model_dump())
        return ApiResponse(data=plan.to_dict())


@router.post("/apply", status_code=202, response_model=ApiResponse[RuntimeOperationResponse])
async def apply_configuration(body: RuntimeApplyRequest, service=Depends(get_runtime_configuration)):
    with _configuration_errors():
        return ApiResponse(data=service.submit(body.plan_id, body.operation_id))


@router.get("/operations/{operation_id}", response_model=ApiResponse[RuntimeOperationResponse])
def configuration_operation(operation_id: str, service=Depends(get_runtime_configuration)):
    try:
        return ApiResponse(data=service.operation(operation_id))
    except KeyError as exc:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Configuration operation not found") from exc
