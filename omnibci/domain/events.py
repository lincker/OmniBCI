"""
Domain Events — omnibci/domain/events.py

Rich domain events emitted by BCISession.
These are *different* from the low-level EventType integers used by the
infrastructure EventBus: those carry bare ints for IPC; these carry full
domain objects and are translated at the infrastructure boundary.
"""

from dataclasses import dataclass
from typing import Optional

from omnibci.domain.classification import Classification


@dataclass(frozen=True)
class DomainEvent:
    """Base for all domain events (isinstance family root)."""


@dataclass(frozen=True)
class TrialCompleted(DomainEvent):
    """
    Raised when a single BCI trial finishes and a Classification is available.

    Attributes:
        session_id:     ID of the BCISession that produced this trial.
        trial_number:   1-based index of this trial within the session.
        classification: The decoder's result.
    """
    session_id: str
    trial_number: int
    classification: Classification
    config_revision: Optional[str] = None

    def __post_init__(self) -> None:
        if self.trial_number < 1:
            raise ValueError(f"trial_number must be ≥ 1, got {self.trial_number}")


@dataclass(frozen=True)
class SessionStarted(DomainEvent):
    """Raised when a BCISession transitions from idle to running."""
    session_id: str
    paradigm: Optional[str] = None
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None
    subject_id: Optional[str] = None


@dataclass(frozen=True)
class SessionEnded(DomainEvent):
    """
    Raised when a BCISession is explicitly closed.

    Attributes:
        session_id:   ID of the closed session.
        total_trials: Number of trials completed in this session.
    """
    session_id: str
    total_trials: int
    config_revision: Optional[str] = None
    interruption_reason: Optional[str] = None
