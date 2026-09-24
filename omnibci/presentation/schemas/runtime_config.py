"""Reviewable runtime configuration plans and observed status."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from omnibci.presentation.schemas.plugin_config import AcceptActivationRequest


class RuntimePlanRequest(AcceptActivationRequest):
    pass


class RuntimePlanResponse(BaseModel):
    plan_id: str
    expected_revision: str | None
    target_revision: str
    applied_revision: str | None
    generation: str | None
    running: bool
    strategy: Literal["select_on_start", "no_change", "trial_boundary", "module", "rebuild"]
    changed_slots: list[str]
    changed_settings: list[str]
    session_effect: Literal["preserve", "continuation"]
    errors: list[str]


class RuntimeOperationResponse(BaseModel):
    operation_id: str
    plan_id: str
    target_revision: str
    status: Literal["queued", "applying", "succeeded", "failed", "recovery_required"]
    stage: str
    error: str | None
    rolled_back: bool
    result: dict[str, Any] | None


class RuntimeApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: str
    operation_id: str


class RuntimeConfigurationResponse(BaseModel):
    running: bool
    recovery_required: bool
    accepted_revision: str | None
    applied_revision: str | None
    generation: str | None
    operation: RuntimeOperationResponse | None
