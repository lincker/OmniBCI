"""Typed contracts for source metadata, processing input, and resolved plans."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from pydantic_core import PydanticCustomError

from omnibci.presentation.schemas.plugin_selection import PluginSelectionResponse
from omnibci.presentation.schemas.acquisition import SignalInfoModel


class SignalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChannelBindingModel(SignalModel):
    name: str
    source_channel_id: str | None = None
    source_channel_index: int | None = Field(default=None, ge=0,
        description="Zero-based signal row, excluding the final event row. Use either this or source_channel_id.")
    signal_type: Literal["eeg", "eog", "ecg", "emg", "misc"] | None = None

    @model_validator(mode="after")
    def source_reference(self):
        if (self.source_channel_id is None) == (self.source_channel_index is None):
            raise ValueError("Channel binding requires exactly one source_channel_id or source_channel_index")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        for key in ("source_channel_id", "source_channel_index"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ProcessingInputModel(SignalModel):
    schema_version: Literal[2] = 2
    outputs: list[ChannelBindingModel] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise PydanticCustomError("schema_version_type", "schema_version must be the integer 2")
        return value


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
    metadata_origin: Literal["connected", "unavailable"]


class SignalSetupResponse(BaseModel):
    selected_source_id: str | None = None
    selected_decoder_profile_id: str | None = None
    selected_decoder_selection: PluginSelectionResponse | None = None
    acquisition_descriptor: SignalInfoModel | None = None
    source_error: str | None = None
    source_binding: ProcessingSourceBindingModel | None = None
    source_revision: str
    processing_input: ProcessingInputModel | None = None
    accepted_input: ProcessingInputModel | None = None
    active_input: ProcessingInputModel | None = None
    revision: str
    readiness: SignalReadinessModel


class SignalPreviewResponse(BaseModel):
    plan: dict[str, Any]
    warnings: list[str]
    source_revision: str
