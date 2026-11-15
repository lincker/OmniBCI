"""Reviewable runtime configuration plans and observed status."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnibci.presentation.schemas.plugin_config import (
    ProfileBindingResponse, SelectProfileBindingRequest,
)
from omnibci.presentation.schemas.plugin_selection import PluginSelectionResponse
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel


class DraftPluginBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot: str
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int | None = Field(default=None, ge=1)
    processing_input: ProcessingInputModel | None = Field(default=None,
        description="Required as an explicit object for processing.decoder; omit for other plugin roles.")


class RuntimePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: Literal["acquisition", "processing", "stimulation", "actuation", "engine"]
    expected_revision: str | None = None
    refresh_slots: list[str] | None = None
    bindings: list[SelectProfileBindingRequest | DraftPluginBindingRequest]
    settings: dict[str, Any] | None = None


class ModuleConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: str | None
    settings: dict[str, Any]
    bindings: list[DraftPluginBindingRequest]


class ModuleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    configuration: ModuleConfigurationRequest


class RuntimePlanResponse(BaseModel):
    plan_id: str
    expected_revision: str | None
    expected_revisions: dict[str, str | None]
    target_revisions: dict[str, str]
    applied_revisions: dict[str, str | None] | None
    generation: str | None
    running: bool
    strategy: Literal["select_on_start", "no_change", "trial_boundary", "module", "rebuild"]
    changed_slots: list[str]
    changed_settings: list[str]
    errors: list[str]
    signal_summary: str | None = None
    module: Literal["acquisition", "processing", "stimulation", "actuation", "engine"]


class RuntimeOperationResponse(BaseModel):
    module: Literal["acquisition", "processing", "stimulation", "actuation", "engine"]
    affected_modules: list[str]
    operation_id: str
    plan_id: str
    target_revisions: dict[str, str]
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
    recovery: dict[str, str]
    accepted: dict[str, str | None]
    applied: dict[str, str | None] | None
    generation: str | None
    operations: dict[str, RuntimeOperationResponse | None]
    errors: dict[str, str] = Field(default_factory=dict)


class NodeConfigurationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format_version: Literal[1]
    role: Literal["stimulation", "actuation"]
    module_revisions: dict[str, str]
    engine_revision: str
    generation: str | None
    settings: dict[str, Any]
    bindings: list[ProfileBindingResponse]


class NodeConfigurationPublicationResponse(BaseModel):
    state: Literal["selected", "running"]
    configuration: NodeConfigurationResponse
