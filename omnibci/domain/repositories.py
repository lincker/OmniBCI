"""
Domain Repository interfaces (Ports).

These ABCs define *what* the domain needs from persistence — not *how* it is
stored.  Infrastructure provides the concrete implementations (Adapters).

Dependency rule: domain/ imports nothing from application/, infrastructure/,
or api/.  Infrastructure depends on domain, never the other way around.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Persisted entity — the storable form of a BCI session visible to the API.
#
# This is NOT the BCISession aggregate root (that lives in the subprocess and
# enforces domain invariants).  SessionRecord is the *projection* of a session
# that the infrastructure layer persists and the application layer queries.
# ---------------------------------------------------------------------------

class SessionRecordState(str, Enum):
    RUNNING = "running"
    ENDED   = "ended"


@dataclass
class TrialRecord:
    """Persisted form of one trial result — a thin value object."""
    trial_number: int
    target_id: int
    event_id: int
    confidence: Optional[float]
    forced: bool
    produced_at: datetime
    config_revision: Optional[str] = None


@dataclass
class SessionRecord:
    """
    Persisted projection of a BCI session.

    Rules:
      - session_id is immutable once set.
      - trials are append-only; existing entries must not be mutated.
      - state transitions: RUNNING → ENDED (one-way).
    """
    session_id: str
    paradigm: Optional[str]
    state: SessionRecordState
    started_at: datetime
    ended_at: Optional[datetime] = None
    trials: List[TrialRecord] = field(default_factory=list)
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None
    interruption_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Mutations (guarded by invariants)
    # ------------------------------------------------------------------

    def append_trial(self, trial: TrialRecord) -> None:
        """Append a trial; raises if session is already ended."""
        if self.state == SessionRecordState.ENDED:
            raise RuntimeError(
                f"Cannot append trial to ended session '{self.session_id}'"
            )
        self.trials.append(trial)

    def mark_ended(self, *, interruption_reason: Optional[str] = None) -> None:
        """Transition to ENDED; idempotent."""
        if self.state != SessionRecordState.ENDED:
            self.state = SessionRecordState.ENDED
            self.ended_at = datetime.now(timezone.utc)
        if interruption_reason is not None:
            self.interruption_reason = interruption_reason

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def trial_count(self) -> int:
        return len(self.trials)

    @property
    def last_trial(self) -> Optional[TrialRecord]:
        return self.trials[-1] if self.trials else None


# ---------------------------------------------------------------------------
# Repository interface (Port)
# ---------------------------------------------------------------------------

class SessionRepository(ABC):
    """
    Abstract repository for SessionRecord persistence.

    Implementations must be thread-safe: the EventBus callbacks run in the
    NeuroEngine thread while HTTP handlers run in the uvicorn event loop.

    Concurrency contract
    --------------------
    Per-method atomicity is not enough: a read-modify-write such as
    ``find_by_id -> mutate copy -> save`` lets two interleaving threads
    overwrite each other wholesale (lost trials, or an ENDED record pushed
    back to RUNNING). Therefore:

      - **cross-thread mutations must go through update() / upsert()** — the
        mutator runs inside the repository's atomicity guarantee;
      - ``save()`` is for inserting brand-new records only (e.g. a
        REST-created session);
      - ``find_*`` stay read-only — returned records are for reading.
    """

    @abstractmethod
    def save(self, record: SessionRecord) -> None:
        """Insert a brand-new record. Cross-thread mutations go through
        update()/upsert() — see the class-level concurrency contract."""

    @abstractmethod
    def find_by_id(self, session_id: str) -> Optional[SessionRecord]:
        """Return the record with the given id, or None if not found."""

    @abstractmethod
    def find_all(self) -> List[SessionRecord]:
        """Return all stored records, ordered by started_at ascending."""

    @abstractmethod
    def find_active(self) -> Optional[SessionRecord]:
        """Return the most recently created RUNNING record, or None."""

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """Remove a record; no-op if not found."""

    @abstractmethod
    def update(
        self,
        session_id: str,
        mutate: Callable[[SessionRecord], None],
    ) -> Optional[SessionRecord]:
        """Atomic read-modify-write: run *mutate* on the record inside the
        repository's atomicity guarantee.

        Returns a copy of the updated record, or None if the record does not
        exist (mutate is not called). All-or-nothing: if mutate raises, the
        exception propagates and the stored record is left unchanged.
        """

    @abstractmethod
    def upsert(
        self,
        session_id: str,
        create: Callable[[], SessionRecord],
        mutate: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        """Atomic create-if-missing, then mutate.

        If the record does not exist, *create* is called first (its returned
        record's session_id must equal *session_id*); then *mutate* is applied
        to the (existing or newly created) record and committed. Returns a copy
        of the committed record. All-or-nothing, same as update().
        """
