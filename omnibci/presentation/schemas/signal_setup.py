"""Typed editing contract for the single saved signal setup."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError


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


class BipolarOutputModel(SignalModel):
    name: str
    positive: str
    negative: str


class ReferenceModel(SignalModel):
    mode: Literal["original", "single", "average", "bipolar"] = "original"
    channels: list[str] = Field(default_factory=list)


class SignalSetupModel(SignalModel):
    schema_version: Literal[1] = 1
    source_id: str
    assignments: list[ChannelAssignmentModel] = Field(default_factory=list)
    outputs: list[str | BipolarOutputModel] = Field(default_factory=list)
    reference: ReferenceModel = Field(default_factory=ReferenceModel)
    excluded_channels: list[str] = Field(default_factory=list)
    original_reference: str | None = None
    template: str | None = None
    manual_metadata: SourceMetadataModel | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise PydanticCustomError("schema_version_type", "schema_version must be the integer 1")
        return value


class SaveSignalSetupRequest(SignalModel):
    setup: SignalSetupModel
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreviewSignalSetupRequest(SignalModel):
    setup: SignalSetupModel
    metadata: SourceMetadataModel | None = None


class SignalReadinessModel(BaseModel):
    ready: bool
    receipt: dict[str, Any] | None = None


class SignalSetupResponse(BaseModel):
    selected_source_id: str | None = None
    source_metadata: SourceMetadataModel | None = None
    setup: SignalSetupModel | None
    revision: str
    accepted_setup: SignalSetupModel | None
    active_setup: SignalSetupModel | None
    readiness: SignalReadinessModel


class SignalPreviewResponse(BaseModel):
    plan: dict[str, Any]
    warnings: list[str]


class SignalTemplatesResponse(BaseModel):
    templates: list[str]


class SignalTemplateResponse(BaseModel):
    name: str
    positions: dict[str, list[float]]
    coordinate_frame: Literal["head"]
    unit: Literal["m"]
    provider: str
