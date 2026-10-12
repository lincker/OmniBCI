"""Typed contracts for source metadata, processing input, and resolved plans."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from omnibci.presentation.schemas.plugin_selection import PluginSelectionResponse


class SignalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SignalChannelModel(SignalModel):
    index: int = Field(ge=0)
    name: str
    signal_type: str = "unknown"
    unit: str | None = None
    reference: str | None = None
    position: list[float] | None = Field(default=None, min_length=3, max_length=3)
    coordinate_frame: str | None = None
    position_provenance: str | None = None
    derivation: list[str] | None = Field(default=None, min_length=2, max_length=2)
    name_provenance: Literal["generated"] | None = None


class SourceMetadataModel(SignalModel):
    source_id: str
    sample_rate: int = Field(gt=0)
    channels: list[SignalChannelModel]
    trigger_row_index: int = Field(ge=1)
    connection_id: str = ""
    original_reference: str | None = None
    native_event: str = "final-row"
    provenance: str = "user-declared"
    signal_channel_count: int | None = None
    output_row_count: int | None = None


class ChannelAssignmentModel(SignalModel):
    source_index: int = Field(ge=0)
    electrode: str
    expected_name: str
    unit: str | None = None
    signal_type: str | None = None


class ChannelBindingModel(SignalModel):
    name: str
    source: str


class BipolarOutputModel(SignalModel):
    name: str
    positive: str
    negative: str


class ReferenceModel(SignalModel):
    mode: Literal["original", "single", "average", "bipolar"] = "original"
    channels: list[str] = Field(default_factory=list)


class ProcessingInputModel(SignalModel):
    schema_version: Literal[1] = 1
    outputs: list[str | ChannelBindingModel | BipolarOutputModel] = Field(default_factory=list)
    reference: ReferenceModel = Field(default_factory=ReferenceModel)
    excluded_channels: list[str] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise PydanticCustomError("schema_version_type", "schema_version must be the integer 1")
        return value


class SignalSetupModel(ProcessingInputModel):
    source_id: str
    assignments: list[ChannelAssignmentModel] = Field(default_factory=list)
    original_reference: str | None = None
    template: str | None = None
    manual_metadata: SourceMetadataModel | None = None


class PreviewSignalSetupRequest(SignalModel):
    processing_input: ProcessingInputModel
    expected_source_revision: str | None = None
    decoder_selection: PluginSelectionResponse | None = None


class SignalReadinessModel(BaseModel):
    ready: bool
    receipt: dict[str, Any] | None = None


class ProcessingSourceBindingModel(BaseModel):
    source_id: str
    profile_id: str | None = None
    profile_name: str | None = None
    profile_revision: int | None = None
    state: Literal["active", "selected"]
    metadata_origin: Literal["connected", "profile", "unavailable"]


class SignalSetupResponse(BaseModel):
    selected_source_id: str | None = None
    selected_decoder_profile_id: str | None = None
    selected_decoder_selection: PluginSelectionResponse | None = None
    source_metadata: SourceMetadataModel | None = None
    source_error: str | None = None
    source_binding: ProcessingSourceBindingModel | None = None
    source_revision: str
    processing_input: ProcessingInputModel | None = None
    accepted_input: ProcessingInputModel | None = None
    active_input: ProcessingInputModel | None = None
    legacy_setup: SignalSetupModel | None = None
    setup: SignalSetupModel | None
    revision: str
    accepted_setup: SignalSetupModel | None
    active_setup: SignalSetupModel | None
    readiness: SignalReadinessModel


class SignalPreviewResponse(BaseModel):
    plan: dict[str, Any]
    warnings: list[str]
    source_revision: str
