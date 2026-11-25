"""HTTP contracts for exact plugin profiles and accepted activation snapshots."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from omnibci.presentation.schemas.plugin_selection import PluginSelectionResponse
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel


PluginKind = Literal["acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector"]
ConfigurationModule = Literal["acquisition", "processing", "stimulation", "actuation"]


class DecoderChannelRequirementsResponse(BaseModel):
    description: str | None = None
    signal_type: str | None = None
    min_count: int = Field(default=1, ge=1)
    required_names: list[str] = Field(default_factory=list)
    default_names: list[str] = Field(default_factory=list)


class PluginConfigurationResponse(BaseModel):
    domain: ConfigurationModule
    selection: PluginSelectionResponse
    kind: PluginKind
    label: str
    description: str | None
    schema_: dict[str, Any] | None = Field(alias="schema")
    configuration_version: int | None
    available: bool
    controls: dict[str, dict[str, Any]] = Field(default_factory=dict)
    channel_requirements: DecoderChannelRequirementsResponse | None = None


class PluginProfileResponse(BaseModel):
    format_version: Literal[2] = 2
    profile_id: str
    name: str
    kind: PluginKind
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    revision: int
    processing_input: ProcessingInputModel | None = None


class CreatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: PluginKind
    selection: PluginSelectionResponse
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    processing_input: ProcessingInputModel | None = None


class UpdatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any]
    expected_revision: int = Field(ge=1)
    processing_input: ProcessingInputModel | None = None


class ProfileBindingResponse(BaseModel):
    slot: str
    kind: PluginKind
    profile_id: str | None = None
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    profile_revision: int
    processing_input: ProcessingInputModel | None = None

    @model_serializer(mode="wrap")
    def serialize_binding(self, handler):
        data = handler(self)
        if self.processing_input is None:
            data.pop("processing_input", None)
        return data


class ModuleActivationResponse(BaseModel):
    module: ConfigurationModule
    revision: str | None
    settings: dict[str, Any]
    bindings: list[ProfileBindingResponse]


class EngineSettingsResponse(BaseModel):
    revision: str | None
    settings: dict[str, Any]


class ModuleHistoryResponse(BaseModel):
    entry_id: str
    revision: str
    saved_at: str
    restored_from: str | None = None
    binding_summary: list[str]


class ProfileHistoryResponse(BaseModel):
    profile_id: str
    revision: int
    saved_at: str
    profile: PluginProfileResponse


class RevertModuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entry_id: str


class RevertProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class SelectProfileBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot: str
    profile_id: str
