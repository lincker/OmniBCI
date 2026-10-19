"""
BCISession — Domain Aggregate Root.

Encapsulates the lifetime of a single BCI usage session:
  - enforces business invariants (can't record a trial unless running, etc.)
  - accumulates Trial results
  - produces domain events that the rest of the system reacts to

The aggregate root is intentionally decoupled from persistence, transport,
and infrastructure.  It holds a list of pending domain events that the
infrastructure layer must drain and publish after each operation.

Usage (inside SessionRuntime or an application service):
    session = BCISession(session_id="abc", paradigm="classic40SSVEP")
    session.start()
    ...
    event = session.record_trial(epoch, classification)   # → TrialCompleted
    for ev in session.pop_events():
        event_bus.put_event(translate_to_infra(ev))
    ...
    session.end()
"""

import uuid
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, List, Optional

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch, EpochInfo
from omnibci.domain.events import (
    DomainEvent,
    SessionEnded,
    SessionStarted,
    TrialCompleted,
)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

class SessionState(Enum):
    IDLE = auto()
    RUNNING = auto()
    ENDED = auto()


@dataclass
class Trial:
    """
    A completed trial within a session.

    Keeps only the Epoch's metadata projection (EpochInfo), not the signal
    matrix — otherwise the aggregate would grow without bound over a long
    session (a whole channels x samples array per trial). The signal itself is
    not retained anywhere after decoding.
    """
    trial_number: int
    epoch_info: EpochInfo
    classification: Classification
    config_revision: Optional[str] = None


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------

def validate_subject_id(subject_id: Optional[str]) -> None:
    if subject_id is not None and (not isinstance(subject_id, str) or not subject_id.strip()):
        raise ValueError("subject_id must be a non-blank string or None")


class BCISession:
    """
    Aggregate root for a BCI session.

    Invariants enforced:
      - Trials may only be recorded while state == RUNNING.
      - A session may only be started once (IDLE → RUNNING).
      - A session may only be ended from RUNNING state.
      - trial_number is monotonically increasing within a session.

    Domain events accumulate in _pending_events and must be drained by the
    caller via pop_events() after each mutating operation.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        paradigm: Optional[str] = None,
        *,
        continuation_of: Optional[str] = None,
        config_revision: Optional[str] = None,
        subject_id: Optional[str] = None,
    ) -> None:
        validate_subject_id(subject_id)
        self._session_id: str = session_id or str(uuid.uuid4())
        if continuation_of == self._session_id:
            raise ValueError("A continuation must have a new session ID")
        self._paradigm: Optional[str] = paradigm
        self._continuation_of = continuation_of
        self._config_revision = config_revision
        self._subject_id = subject_id
        self._interruption_reason: Optional[str] = None
        self._state: SessionState = SessionState.IDLE
        self._trials: List[Trial] = []
        self._pending_events: List[DomainEvent] = []

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def paradigm(self) -> Optional[str]:
        return self._paradigm

    @property
    def subject_id(self) -> Optional[str]:
        return self._subject_id

    @property
    def continuation_of(self) -> Optional[str]:
        return self._continuation_of

    @property
    def config_revision(self) -> Optional[str]:
        return self._config_revision

    @property
    def interruption_reason(self) -> Optional[str]:
        return self._interruption_reason

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def trial_count(self) -> int:
        return len(self._trials)

    @property
    def trials(self) -> List[Trial]:
        return list(self._trials)  # defensive copy

    # ------------------------------------------------------------------
    # Commands (state-mutating operations)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Transition IDLE → RUNNING.
        Raises RuntimeError if the session is not in IDLE state.
        """
        if self._state != SessionState.IDLE:
            raise RuntimeError(
                f"Cannot start session '{self._session_id}': "
                f"current state is {self._state.name}"
            )
        self._state = SessionState.RUNNING
        self._pending_events.append(
            SessionStarted(
                session_id=self._session_id,
                paradigm=self._paradigm,
                continuation_of=self._continuation_of,
                config_revision=self._config_revision,
                subject_id=self._subject_id,
            )
        )

    def record_trial(
        self, epoch: Epoch | EpochInfo, classification: Classification,
        *, config_revision: Optional[str] = None,
    ) -> TrialCompleted:
        """
        Record a completed trial from an Epoch or its metadata and emit TrialCompleted.

        Business rules:
          - Session must be RUNNING.
          - epoch.event_id must match classification.event_id.

        Returns the TrialCompleted domain event for convenience (also queued
        internally and returned via pop_events()).
        """
        if self._state != SessionState.RUNNING:
            raise RuntimeError(
                f"Cannot record trial: session '{self._session_id}' "
                f"is {self._state.name}, not RUNNING"
            )
        if epoch.event_id != classification.event_id:
            raise ValueError(
                f"Epoch event_id ({epoch.event_id}) does not match "
                f"Classification event_id ({classification.event_id})"
            )

        trial_number = len(self._trials) + 1
        revision = config_revision if config_revision is not None else self._config_revision
        trial = Trial(
            trial_number=trial_number,
            # Trial history keeps only metadata, never the signal matrix.
            epoch_info=epoch if isinstance(epoch, EpochInfo) else EpochInfo.from_epoch(epoch),
            classification=classification,
            config_revision=revision,
        )
        self._trials.append(trial)

        event = TrialCompleted(
            session_id=self._session_id,
            trial_number=trial_number,
            classification=classification,
            config_revision=revision,
        )
        self._pending_events.append(event)
        return event

    def end(self, *, interruption_reason: Optional[str] = None) -> None:
        """
        Transition RUNNING → ENDED.
        Raises RuntimeError if the session is not RUNNING.
        """
        if self._state != SessionState.RUNNING:
            raise RuntimeError(
                f"Cannot end session '{self._session_id}': "
                f"current state is {self._state.name}"
            )
        self._state = SessionState.ENDED
        self._interruption_reason = interruption_reason
        self._pending_events.append(
            SessionEnded(
                session_id=self._session_id,
                total_trials=len(self._trials),
                config_revision=self._config_revision,
                interruption_reason=interruption_reason,
            )
        )

    # ------------------------------------------------------------------
    # Event draining
    # ------------------------------------------------------------------

    def pop_events(self) -> List[DomainEvent]:
        """
        Return and clear all accumulated domain events.
        Must be called by the infrastructure layer after each command to
        publish events to the EventBus.
        """
        events = list(self._pending_events)
        self._pending_events.clear()
        return events

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def last_classification(self) -> Optional[Classification]:
        """Return the most recent classification, or None if no trials yet."""
        if not self._trials:
            return None
        return self._trials[-1].classification

    def accuracy(self, ground_truth: Callable[[Trial], int]) -> Optional[float]:
        """
        Compute recognition accuracy given a ground-truth function.

        Args:
            ground_truth: callable that maps a Trial to the correct target_id.

        Returns:
            Accuracy in [0, 1], or None if no trials have been recorded.
        """
        if not self._trials:
            return None
        correct = sum(
            1 for t in self._trials if t.classification.target_id == ground_truth(t)
        )
        return correct / len(self._trials)

    def __repr__(self) -> str:
        return (
            f"BCISession(id='{self._session_id}', "
            f"state={self._state.name}, "
            f"trials={len(self._trials)}, "
            f"paradigm={self._paradigm!r})"
        )
