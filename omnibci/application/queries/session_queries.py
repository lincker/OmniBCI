"""
Session read-side queries.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import List, Optional

from omnibci.domain.classification import Classification


class SessionStateDTO(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(frozen=True)
class GetSessionQuery:
    session_id: str


@dataclass(frozen=True)
class ListSessionsQuery:
    pass


@dataclass(frozen=True)
class GetSessionTrialsQuery:
    session_id: str


@dataclass(frozen=True)
class GetLastClassificationQuery:
    session_id: str


# ------------------------------------------------------------------
# Result DTOs (read model — no domain invariants)
# ------------------------------------------------------------------

@dataclass
class TrialDTO:
    trial_number: int
    target_id: int
    event_id: int
    confidence: Optional[float]
    forced: bool
    produced_at: datetime
    config_revision: Optional[str] = None


@dataclass
class SessionDTO:
    session_id: str
    paradigm: Optional[str]
    state: SessionStateDTO
    trial_count: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    trials: List[TrialDTO] = field(default_factory=list)
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None
    interruption_reason: Optional[str] = None
    signal_plan: Optional[dict] = None
    subject_id: Optional[str] = None
    recording: Optional[dict] = None
