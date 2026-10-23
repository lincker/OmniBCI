"""Live decoded results for the current engine generation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class TrialResponse(BaseModel):
    generation: str
    trial_number: int
    target_id: int
    event_id: int
    confidence: float | None = None
    forced: bool
    produced_at: datetime
    config_revision: str | None = None


class ResultsResponse(BaseModel):
    generation: str | None
    trials: list[TrialResponse]


class EpochResult(BaseModel):
    target_id: int
    confidence: float | None = None
    forced: bool


class EpochSummary(BaseModel):
    generation: str
    epoch_number: int
    event_id: int | None
    config_revision: str | None
    status: Literal["decoded", "no_result", "interrupted", "error"]
    reason: Literal["prediction", "end_marker", "next_start", "stream_stop", "processor_stop", "error"]
    sample_rate: int | None
    channel_count: int
    sample_count: int
    pre_trigger_samples: int
    started_at: datetime
    completed_at: datetime
    trial_number: int | None
    result: EpochResult | None
    error: str | None


class EpochListResponse(BaseModel):
    generation: str | None
    total: int
    offset: int
    limit: int
    epochs: list[EpochSummary]
