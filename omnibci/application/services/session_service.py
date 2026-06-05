"""
SessionAppService — Application Service (refactored to use SessionRepository).

All session state is now persisted via a SessionRepository (port).
The service holds no mutable state of its own — it is a pure orchestrator
that translates commands/queries into repository operations and EventBus
callbacks.

Dependency graph (follows DIP):
  api → application → domain (repository interface)
                    ↑
  infrastructure implements domain.SessionRepository
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from omnibci.application.commands.session_commands import (
    CreateSessionCommand,
    EndSessionCommand,
)
from omnibci.application.queries.session_queries import (
    GetLastClassificationQuery,
    GetSessionQuery,
    GetSessionTrialsQuery,
    ListSessionsQuery,
    SessionDTO,
    SessionStateDTO,
    TrialDTO,
)
from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.domain.repositories import (
    SessionRecord,
    SessionRepository,
    SessionState,
    TrialRecord,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: translate between domain records and application DTOs
# ---------------------------------------------------------------------------

def _trial_record_to_dto(r: TrialRecord) -> TrialDTO:
    return TrialDTO(
        trial_number=r.trial_number,
        target_id=r.target_id,
        event_id=r.event_id,
        confidence=r.confidence,
        forced=r.forced,
        produced_at=r.produced_at,
    )


def _session_record_to_dto(r: SessionRecord, include_trials: bool = False) -> SessionDTO:
    state = (SessionStateDTO.ENDED if r.state == SessionState.ENDED
             else SessionStateDTO.RUNNING)
    return SessionDTO(
        session_id=r.session_id,
        paradigm=r.paradigm,
        state=state,
        trial_count=r.trial_count,
        started_at=r.started_at,
        ended_at=r.ended_at,
        trials=[_trial_record_to_dto(t) for t in r.trials] if include_trials else [],
    )


# ---------------------------------------------------------------------------
# Application Service
# ---------------------------------------------------------------------------

class SessionAppService:
    """
    Application service for the Session bounded context.

    Constructor requires a SessionRepository instance (injected by DI).
    The default value is removed intentionally — callers must provide one
    so the dependency is explicit and swappable (e.g. in tests).
    """

    def __init__(self, repository: SessionRepository) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_create_session(self, cmd: CreateSessionCommand) -> str:
        session_id = str(uuid.uuid4())
        record = SessionRecord(
            session_id=session_id,
            paradigm=cmd.paradigm,
            state=SessionState.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._repo.save(record)
        logger.info("Session created: %s (paradigm=%s)", session_id, cmd.paradigm)
        return session_id

    def handle_end_session(self, cmd: EndSessionCommand) -> None:
        record = self._require(cmd.session_id)
        record.mark_ended()       # idempotent; sets state + ended_at
        self._repo.save(record)
        logger.info("Session ended: %s (%d trials)", cmd.session_id, record.trial_count)

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_session(self, query: GetSessionQuery) -> SessionDTO:
        return _session_record_to_dto(self._require(query.session_id))

    def handle_list_sessions(self, query: ListSessionsQuery) -> List[SessionDTO]:
        return [_session_record_to_dto(r) for r in self._repo.find_all()]

    def handle_get_trials(self, query: GetSessionTrialsQuery) -> List[TrialDTO]:
        return [_trial_record_to_dto(t) for t in self._require(query.session_id).trials]

    def handle_get_last_classification(
        self, query: GetLastClassificationQuery
    ) -> Optional[TrialDTO]:
        last = self._require(query.session_id).last_trial
        return _trial_record_to_dto(last) if last else None

    @property
    def active_session_id(self) -> Optional[str]:
        record = self._repo.find_active()
        return record.session_id if record else None

    # ------------------------------------------------------------------
    # EventBus callbacks (sync — called from NeuroEngine thread)
    # ------------------------------------------------------------------

    def on_trial_completed(self, event: Event) -> None:
        """
        Receive EventType.TrialCompleted and append the trial to the active
        session in the repository.
        """
        # Payload is a TrialOutcome (session_id + trial_number + classification);
        # tolerate a bare Classification for backward compatibility.
        payload = event.data
        clf: Classification = getattr(payload, "classification", payload)
        if not isinstance(clf, Classification):
            logger.error("on_trial_completed: expected Classification, got %s", type(clf))
            return

        record = self._repo.find_active()
        if record is None:
            logger.warning("TrialCompleted received but no active session; ignoring")
            return

        trial_number = record.trial_count + 1
        record.append_trial(TrialRecord(
            trial_number=trial_number,
            target_id=clf.target_id,
            event_id=clf.event_id,
            confidence=clf.confidence,
            forced=clf.forced,
            produced_at=clf.produced_at,
        ))
        self._repo.save(record)
        logger.debug("Trial #%d recorded: session=%s target=%d",
                     trial_number, record.session_id, clf.target_id)

    def on_session_ended(self, event: Event) -> None:
        """Receive EventType.SessionEnded and close the active session."""
        record = self._repo.find_active()
        if record is None:
            return
        self.handle_end_session(EndSessionCommand(session_id=record.session_id))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require(self, session_id: str) -> SessionRecord:
        record = self._repo.find_by_id(session_id)
        if record is None:
            raise KeyError(f"Session '{session_id}' not found")
        return record
