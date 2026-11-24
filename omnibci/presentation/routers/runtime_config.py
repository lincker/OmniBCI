"""Runtime configuration planning; no profile save implicitly applies changes."""
from typing import Literal
from fastapi import APIRouter, Depends

from omnibci.presentation.dependencies import (
    get_node_configuration_service, get_runtime_configuration,
)
from omnibci.presentation.errors import ApiError, configuration_errors
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.plugin_config import ConfigurationModule, RevertModuleRequest
from omnibci.presentation.schemas.runtime_config import (
    RuntimeConfigurationResponse, RuntimePlanRequest, RuntimePlanResponse,
    RuntimeApplyRequest, RuntimeOperationResponse,
    NodeConfigurationPublicationResponse,
)

router = APIRouter(prefix="/config", tags=["Runtime configuration"])


@router.get("/nodes/{role}", response_model=ApiResponse[NodeConfigurationPublicationResponse])
def node_configuration(
    role: Literal["stimulation", "actuation"],
    service=Depends(get_node_configuration_service),
):
    with configuration_errors():
        return ApiResponse(data=service.configuration(role))


@router.get("/runtime", response_model=ApiResponse[RuntimeConfigurationResponse])
def runtime_status(service=Depends(get_runtime_configuration)):
    with configuration_errors():
        return ApiResponse(data=service.status())


@router.post("/apply-plan", response_model=ApiResponse[RuntimePlanResponse])
def plan_configuration(body: RuntimePlanRequest, service=Depends(get_runtime_configuration)):
    with configuration_errors():
        plan = service.plan(**body.model_dump())
        return ApiResponse(data=plan.to_dict())


@router.post("/apply", status_code=202, response_model=ApiResponse[RuntimeOperationResponse])
async def apply_configuration(body: RuntimeApplyRequest, service=Depends(get_runtime_configuration)):
    with configuration_errors():
        return ApiResponse(data=service.submit(body.plan_id, body.operation_id))


@router.get("/operations/{operation_id}", response_model=ApiResponse[RuntimeOperationResponse])
def configuration_operation(operation_id: str, service=Depends(get_runtime_configuration)):
    try:
        return ApiResponse(data=service.operation(operation_id))
    except KeyError as exc:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Configuration operation not found") from exc


@router.post("/modules/{module}/revert-plan", response_model=ApiResponse[RuntimePlanResponse])
def revert_module(module: ConfigurationModule, body: RevertModuleRequest,
                  service=Depends(get_runtime_configuration)):
    with configuration_errors():
        try:
            return ApiResponse(data=service.revert_plan(module, body.entry_id).to_dict())
        except KeyError as exc:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Module history entry not found") from exc
