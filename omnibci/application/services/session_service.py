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

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

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
from omnibci.application.events import SessionSummary, TrialOutcome
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.domain.repositories import (
    SessionRecord,
    SessionRepository,
    SessionRecordState,
    TrialRecord,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from omnibci.application.services.command_result_service import CommandResultBroker


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
    state = (SessionStateDTO.ENDED if r.state == SessionRecordState.ENDED
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

    ``engine_port`` (optional) lets create/end commands push the session
    identity down into the signal-processing subprocess (StartSession /
    EndSession events), so the aggregate in the worker and the read-model
    here share ONE session_id.  None = projection-only mode (tests).
    """

    def __init__(
        self,
        repository: SessionRepository,
        engine_port=None,
        command_results: "CommandResultBroker | None" = None,
    ) -> None:
        self._repo = repository
        self._engine = engine_port  # EnginePort | None
        self._command_results = command_results

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_create_session(self, cmd: CreateSessionCommand) -> str:
        session_id = str(uuid.uuid4())
        record = SessionRecord(
            session_id=session_id,
            paradigm=cmd.paradigm,
            state=SessionRecordState.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._repo.save(record)

        # Push the identity down into the signal-processing subprocess so its
        # BCISession aggregate uses the SAME id (see docs/session-id-unification.md).
        # On dispatch failure roll the record back — a session the engine never
        # heard of would just resurrect the old two-id split.
        if self._command_results is not None:
            try:
                self._command_results.request(
                    EventType.StartSession,
                    {"session_id": session_id, "paradigm": cmd.paradigm},
                )
            except CommandTimeoutError:
                # Timeout is an unknown outcome, not a confirmed failure: the
                # worker may have started this exact session and its late
                # SessionStarted event will reconcile the projection.
                raise
            except BaseException:
                self._repo.delete(session_id)
                raise
        elif self._engine is not None:
            try:
                self._engine.dispatch(Event(
                    EventType.StartSession,
                    {"session_id": session_id, "paradigm": cmd.paradigm},
                ))
            except RuntimeError:
                self._repo.delete(session_id)
                raise

        logger.info("Session created: %s (paradigm=%s)", session_id, cmd.paradigm)
        return session_id

    def handle_end_session(self, cmd: EndSessionCommand) -> None:
        if self._repo.find_by_id(cmd.session_id) is None:
            raise KeyError(f"Session '{cmd.session_id}' not found")

        # Confirm the aggregate ended before the read model reports success.
        # SessionEnded is queued before CommandResult by the worker, so the
        # projection is normally already closed when request() returns; the
        # atomic mark below is an idempotent fallback.
        if self._command_results is not None:
            self._command_results.request(EventType.EndSession, cmd.session_id)
        elif self._engine is not None:
            # Projection-only/legacy constructions retain best-effort dispatch.
            try:
                self._engine.dispatch(Event(EventType.EndSession, cmd.session_id))
            except RuntimeError as exc:
                logger.warning("EndSession dispatch skipped (engine not ready): %s", exc)

        # Keep ending atomic with concurrent trial projection updates.
        record = self._repo.update(cmd.session_id, lambda r: r.mark_ended())
        if record is None:  # defensive: repository deletion raced the command
            raise KeyError(f"Session '{cmd.session_id}' not found")
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

    def on_session_started(self, event: Event) -> None:
        """
        Receive EventType.SessionStarted (payload: session_id str) and make
        sure a RUNNING record exists for that id (create-if-missing projection;
        no-op when REST already created it — the normal flow).
        """
        session_id = event.data
        if not isinstance(session_id, str) or not session_id:
            logger.error("on_session_started: bad payload %r", session_id)
            return
        # Do not overwrite a record that REST created concurrently.
        created = False

        def _create() -> SessionRecord:
            nonlocal created
            created = True
            return SessionRecord(
                session_id=session_id,
                paradigm=None,
                state=SessionRecordState.RUNNING,
                started_at=datetime.now(timezone.utc),
            )

        self._repo.upsert(session_id, _create, lambda r: None)
        if created:
            logger.info("Session projected from engine event: %s", session_id)

    def on_trial_completed(self, event: Event) -> None:
        """
        Receive EventType.TrialCompleted and append the trial to the session
        identified by the payload's session_id.  A record is created on the
        fly for an unknown id (projection must never lose trials).

        The payload must be a TrialOutcome with explicit session identity.
        """
        payload = event.data
        if not isinstance(payload, TrialOutcome):
            logger.error("on_trial_completed: expected TrialOutcome, got %s",
                         type(payload).__name__)
            return
        clf: Classification = payload.classification

        # Compute the number and append under the repository lock.
        # Prefer the aggregate's own trial_number (TrialOutcome) over a local
        # count so read model and aggregate can never drift.
        def _append(record: SessionRecord) -> None:
            # Zero means the producer could not provide an aggregate sequence.
            trial_number = payload.trial_number or record.trial_count + 1
            record.append_trial(TrialRecord(
                trial_number=trial_number,
                target_id=clf.target_id,
                event_id=clf.event_id,
                confidence=clf.confidence,
                forced=clf.forced,
                produced_at=clf.produced_at,
            ))
            logger.debug("Trial #%d recorded: session=%s target=%d",
                         trial_number, record.session_id, clf.target_id)

        session_id = payload.session_id

        def _create() -> SessionRecord:
            # Engine session unknown to the read model — project it now
            # rather than dropping or mis-filing the trial.
            logger.warning("TrialCompleted for unknown session %s; creating record",
                           session_id)
            return SessionRecord(
                session_id=session_id,
                paradigm=None,
                state=SessionRecordState.RUNNING,
                started_at=datetime.now(timezone.utc),
            )

        self._repo.upsert(session_id, _create, _append)

    def on_session_ended(self, event: Event) -> None:
        """
        Receive EventType.SessionEnded and close the session identified by the
        payload.

        The payload must be a SessionSummary with explicit session identity.
        """
        payload = event.data
        if not isinstance(payload, SessionSummary):
            logger.error("on_session_ended: expected SessionSummary, got %s",
                         type(payload).__name__)
            return
        # Keep ending atomic with concurrent trial projection updates.
        record = self._repo.update(payload.session_id, lambda r: r.mark_ended())
        if record is None:
            logger.warning(
                "SessionEnded for unknown session %s; ignoring",
                payload.session_id,
            )
            return
        logger.info("Session ended (projected): %s (%d trials)",
                    record.session_id, record.trial_count)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require(self, session_id: str) -> SessionRecord:
        record = self._repo.find_by_id(session_id)
        if record is None:
            raise KeyError(f"Session '{session_id}' not found")
        return record
