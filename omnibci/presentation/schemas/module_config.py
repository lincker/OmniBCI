"""Local module profile contracts; identity is assigned by the backend."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModuleProfileResponse(BaseModel):
    schema_version: Literal[1] = 1
    profile_id: str
    name: str
    module: str
    plugin_type: str
    parameters: dict[str, Any]


class ModuleProfileListResponse(BaseModel):
    profiles: list[ModuleProfileResponse]


class ModuleConfigField(BaseModel):
    key: str
    label: str
    type: Literal["int", "str"]
    default: int | str
    description: str
    minimum: int | None = None
    maximum: int | None = None


class ModuleConfigPlugin(BaseModel):
    plugin_type: str
    label: str
    fields: list[ModuleConfigField]


class ModuleConfigResponse(BaseModel):
    module: str
    active_profile_id: str | None
    effective_plugin_type: str | None
    effective_parameters: dict[str, Any]
    plugins: list[ModuleConfigPlugin]
    selection_error: str | None = None


class UpdateModuleProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(description="Complete plugin parameters, not a patch")


class CreateModuleProfileRequest(UpdateModuleProfileRequest):
    plugin_type: str


class SelectModuleProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = Field(..., description="Profile UUID; null restores legacy configuration")
