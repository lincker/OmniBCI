"""
Session API schemas (request / response DTOs).
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from omnibci.application.queries.session_queries import SessionStateDTO


class CreateSessionRequest(BaseModel):
    paradigm: Optional[str] = Field(None, description="Paradigm name; defaults to config value")


class TrialResponse(BaseModel):
    trial_number: int
    target_id: int
    event_id: int
    confidence: Optional[float] = None
    forced: bool
    produced_at: datetime
    config_revision: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    paradigm: Optional[str]
    state: SessionStateDTO
    trial_count: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None
    interruption_reason: Optional[str] = None


class SessionDetailResponse(SessionResponse):
    """SessionResponse extended with full trial list."""
    trials: List[TrialResponse] = []


class SessionListResponse(BaseModel):
    sessions: List[SessionResponse]
    total: int


class SessionExportResponse(BaseModel):
    format_version: Literal[1] = 1
    exported_at: datetime
    app_version: str
    sessions: List[SessionDetailResponse]
