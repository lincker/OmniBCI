"""Reviewable runtime configuration plans and observed status."""
from typing import Any, Literal

from pydantic import BaseModel

from omnibci.presentation.schemas.plugin_config import AcceptActivationRequest


class RuntimePlanRequest(AcceptActivationRequest):
    include_host_settings: bool = False


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


class RuntimeConfigurationResponse(BaseModel):
    running: bool
    accepted_revision: str | None
    applied_revision: str | None
    generation: str | None
    operation: dict[str, Any] | None
