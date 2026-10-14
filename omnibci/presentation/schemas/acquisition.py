from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class ChannelDescriptorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel_id: str
    index: int = Field(ge=0)
    name: str
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: Literal["generated"] | None = None


class AcquisitionDescriptorModel(BaseModel):
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


class AcquisitionMetadataResponse(BaseModel):
    source_id: str
    connection_id: str
    metadata: str
    descriptor: AcquisitionDescriptorModel | None


class AcquisitionStatusResponse(BaseModel):
    connected: bool
    streaming: bool
    source: Optional[str] = None
    sample_rate: Optional[int] = None
    channel_count: Optional[int] = None
    metadata_available: bool = False
    metadata_provenance: Optional[str] = None
    connection_id: Optional[str] = None
