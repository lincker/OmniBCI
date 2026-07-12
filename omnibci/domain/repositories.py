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

    def mark_ended(self) -> None:
        """Transition to ENDED; idempotent."""
        if self.state != SessionRecordState.ENDED:
            self.state = SessionRecordState.ENDED
            self.ended_at = datetime.now(timezone.utc)

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

    并发契约（P1-4）
    ----------------
    单个方法各自原子并不足够：``find_by_id → 改副本 → save`` 这样的
    读-改-写组合在两个线程交错时会整体覆盖对方的修改（丢 trial、或
    ENDED 状态被顶回 RUNNING）。因此：

      - **跨线程的修改必须走 update() / upsert()** —— mutator 在仓储的
        原子性保证内执行；
      - ``save()`` 仅用于插入全新记录（如 REST 创建会话）；
      - ``find_*`` 保持只读语义，返回的记录仅供读取。
    """

    @abstractmethod
    def save(self, record: SessionRecord) -> None:
        """Insert or replace the given SessionRecord."""

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
        """原子读-改-写：在仓储的原子性保证内对记录执行 *mutate*。

        返回更新后的记录拷贝；记录不存在返回 None（mutate 不执行）。
        All-or-nothing：mutate 抛出异常时原样传播，存储的记录保持不变。
        """

    @abstractmethod
    def upsert(
        self,
        session_id: str,
        create: Callable[[], SessionRecord],
        mutate: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        """原子 create-if-missing-then-mutate。

        记录不存在时先调用 *create*（其返回记录的 session_id 必须等于
        *session_id*），随后对（既有或新建的）记录执行 *mutate* 并提交。
        返回提交后的记录拷贝。All-or-nothing 语义同 update()。
        """
