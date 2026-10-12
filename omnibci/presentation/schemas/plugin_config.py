"""HTTP contracts for exact plugin profiles and accepted activation snapshots."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from omnibci.presentation.schemas.plugin_selection import PluginSelectionResponse
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel, SourceMetadataModel


PluginKind = Literal["acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector"]


class DecoderChannelRequirementsResponse(BaseModel):
    description: str | None = None
    signal_type: str | None = None
    min_count: int = Field(default=1, ge=1)
    required_names: list[str] = Field(default_factory=list)
    default_names: list[str] = Field(default_factory=list)


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
    acquisition_montage: SourceMetadataModel | None = Field(default=None, json_schema_extra={"readOnly": True})
    manual_montage: SourceMetadataModel | None = None
    processing_input: ProcessingInputModel | None = None


class CreatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: PluginKind
    selection: PluginSelectionResponse
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    manual_montage: SourceMetadataModel | None = None
    processing_input: ProcessingInputModel | None = None


class UpdatePluginProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any]
    expected_revision: int = Field(ge=1)
    manual_montage: SourceMetadataModel | None = None
    processing_input: ProcessingInputModel | None = None


class ProfileBindingResponse(BaseModel):
    slot: str
    kind: PluginKind
    profile_id: str | None = None
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    profile_revision: int
    manual_montage: SourceMetadataModel | None = None
    processing_input: ProcessingInputModel | None = None

    @model_serializer(mode="wrap")
    def serialize_binding(self, handler):
        data = handler(self)
        if self.manual_montage is None:
            data.pop("manual_montage", None)
        if self.processing_input is None:
            data.pop("processing_input", None)
        return data


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
    include_host_settings: bool = False
