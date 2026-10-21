"""Live decoded results for the current engine generation."""

from datetime import datetime

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
