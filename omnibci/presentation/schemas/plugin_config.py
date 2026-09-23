"""HTTP contracts for exact plugin profiles and accepted activation snapshots."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PluginKind = Literal["acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector"]


class PluginSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_id: str
    contribution_id: str
    version: str | None = None


class PluginConfigurationResponse(BaseModel):
    selection: PluginSelectionResponse
    kind: PluginKind
    plugin_type: str
    label: str
    description: str | None
    schema_: dict[str, Any] | None = Field(alias="schema")
    configuration_version: int | None
    available: bool
    controls: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PluginProfileResponse(BaseModel):
    format_version: Literal[2] = 2
    profile_id: str
    name: str
    kind: PluginKind
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    revision: int


class CreatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: PluginKind
    selection: PluginSelectionResponse
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)


class UpdatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any]
    expected_revision: int = Field(ge=1)


class ProfileBindingResponse(BaseModel):
    slot: str
    kind: PluginKind
    profile_id: str | None = None
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    profile_revision: int


class ActivationResponse(BaseModel):
    format_version: Literal[2] = 2
    revision: str
    settings: dict[str, Any]
    bindings: list[ProfileBindingResponse]


class SelectProfileBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot: str
    profile_id: str


class AcceptActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bindings: list[SelectProfileBindingRequest]
    expected_revision: str | None = None
    refresh_slots: list[str] | None = None
