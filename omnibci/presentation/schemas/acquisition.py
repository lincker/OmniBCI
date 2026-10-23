from typing import Annotated, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from omnibci.application.acquisition_preview import MAX_CHUNKS, MAX_ROWS, MAX_SAMPLES, PreviewSelection


class ChannelDescriptorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel_id: str
    index: int = Field(ge=0)
    name: str
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: Literal["generated"] | None = None


class SignalInfoModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    sample_rate: int = Field(gt=0)
    channels: list[ChannelDescriptorModel]
    trigger_row_index: int = Field(ge=1)
    connection_id: str = ""
    native_event: str = "final-row"
    provenance: str = "protocol-defined"
    signal_channel_count: int | None = None
    output_row_count: int | None = None


class SourceParameterComparisonModel(BaseModel):
    expected: int | None
    actual: int


class SourceExpectationsModel(BaseModel):
    status: Literal["unconfirmed", "matched", "mismatched", "unverified"]
    sample_rate: SourceParameterComparisonModel
    channel_count: SourceParameterComparisonModel


class AcquisitionMetadataResponse(BaseModel):
    source_id: str
    connection_id: str
    metadata: str
    descriptor: SignalInfoModel | None
    comparison: SourceExpectationsModel | None = None


class AcquisitionStatusResponse(BaseModel):
    connected: bool
    streaming: bool
    source: Optional[str] = None
    sample_rate: Optional[int] = None
    channel_count: Optional[int] = None
    metadata_available: bool = False
    metadata_provenance: Optional[str] = None
    connection_id: Optional[str] = None


class AcquisitionPreviewSelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rows: list[Annotated[int, Field(ge=0)]] = Field(default_factory=lambda: [0], max_length=MAX_ROWS)
    sample_start: int = Field(default=0, ge=0)
    sample_count: int = Field(default=128, ge=1, le=MAX_SAMPLES)
    include_trigger: bool = True
    chunk_count: int = Field(default=MAX_CHUNKS, ge=1, le=MAX_CHUNKS)

    @model_validator(mode="after")
    def valid_selection(self):
        try:
            PreviewSelection.from_value(self.model_dump())
        except ValueError as exc:
            raise PydanticCustomError("preview_selection", str(exc)) from exc
        return self


class StartAcquisitionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: str = Field(min_length=1, max_length=128)
    expected_revision: str = Field(pattern="^[0-9a-f]{64}$")
    connection_id: str = Field(min_length=1, max_length=128)
    selection: AcquisitionPreviewSelectionModel = Field(default_factory=AcquisitionPreviewSelectionModel)
    continuous: bool = False


class StopAcquisitionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: str = Field(min_length=1, max_length=128)
    capture_id: str = Field(min_length=1, max_length=128)


class AcquisitionPreviewLimitsModel(BaseModel):
    max_chunks: int
    max_rows: int
    max_samples: int
    max_values: int
    max_frame_bytes: int
    max_cache_bytes: int
    preview_queue_size: int
    capture_timeout_seconds: float


class AcquisitionPreviewChunkSummary(BaseModel):
    sequence_id: int
    timestamp_ns: str
    original_shape: list[int] = Field(min_length=2, max_length=2)
    dtype: str
    row_indices: list[int]
    sample_start: int
    sample_count: int
    trigger_row_index: int
    encoded_bytes: int


class AcquisitionPreviewResponse(BaseModel):
    """Capture-time context and independently sampled producer/cache counters."""

    capture_id: str | None
    generation: str
    config_revision: str | None
    source_id: str | None
    connection_id: str | None
    status: Literal["idle", "capturing", "finished"]
    continuous: bool = False
    reason: str | None
    selection: AcquisitionPreviewSelectionModel | None
    observed: int
    enqueued: int
    dropped: int
    oversized: int
    enqueued_bytes: int
    limits: AcquisitionPreviewLimitsModel
    stream_chunk_count: int
    stream_sample_count: int
    last_sequence: int
    available: bool
    cache_errors: int
    received: int
    retained_bytes: int
    chunks: list[AcquisitionPreviewChunkSummary]


class AcquisitionPreviewChunkResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    capture_id: str
    generation: str
    config_revision: str | None
    source_id: str
    connection_id: str
    sequence_id: int
    timestamp_ns: str
    original_shape: list[int] = Field(min_length=2, max_length=2)
    dtype: str
    row_indices: list[int]
    sample_start: int
    sample_count: int
    trigger_row_index: int
    values: list[list[int | float | bool | str]]
    descriptor: SignalInfoModel | None
