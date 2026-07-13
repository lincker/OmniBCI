"""
Domain Events — omnibci/domain/events.py

Rich domain events emitted by BCISession.
These are *different* from the low-level EventType integers used by the
infrastructure EventBus: those carry bare ints for IPC; these carry full
domain objects and are translated at the infrastructure boundary.

Design note — `occurred_at` uses `field(init=False)`:
  Dataclass inheritance rule: fields with defaults must not precede fields
  without defaults in the combined __init__ signature.  Since DomainEvent's
  `occurred_at` has a default_factory and subclasses add fields without
  defaults, the only clean fix is `init=False`: the field is never an __init__
  parameter — the dataclass-generated __init__ populates it from the
  default_factory *before* any __post_init__ runs.  Subclasses' own
  __post_init__ therefore need do nothing for `occurred_at`, and must NOT call
  ``super().__post_init__()`` — DomainEvent defines no __post_init__ (calling
  it would raise AttributeError).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch


@dataclass(frozen=True)
class DomainEvent:
    """Base for all domain events."""
    # init=False: never a constructor parameter; the generated __init__ sets it
    # from the default_factory (not __post_init__).
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
        init=False,
    )


@dataclass(frozen=True)
class TrialCompleted(DomainEvent):
    """
    Raised when a single BCI trial finishes and a Classification is available.

    Attributes:
        session_id:     ID of the BCISession that produced this trial.
        trial_number:   1-based index of this trial within the session.
        epoch:          The raw EEG segment that was classified.
                        Transient: valid only while the event is being
                        delivered. The aggregate's trial history keeps only
                        EpochInfo metadata, so subscribers must not retain a
                        long-lived reference to this event (or its epoch) —
                        consume/persist the signal while handling the event.
        classification: The decoder's result.
    """
    session_id: str
    trial_number: int
    epoch: Epoch
    classification: Classification

    def __post_init__(self) -> None:
        if self.trial_number < 1:
            raise ValueError(f"trial_number must be ≥ 1, got {self.trial_number}")


@dataclass(frozen=True)
class SessionStarted(DomainEvent):
    """Raised when a BCISession transitions from idle to running."""
    session_id: str
    paradigm: Optional[str] = None
    # Inherits DomainEvent.__post_init__ — sets occurred_at automatically.


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
