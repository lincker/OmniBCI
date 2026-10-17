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
from copy import deepcopy
from threading import RLock
from omnibci.application.runtime_admission import admitted
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
        config_revision=r.config_revision,
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
        continuation_of=r.continuation_of,
        config_revision=r.config_revision,
        interruption_reason=r.interruption_reason,
        signal_plan=deepcopy(r.signal_plan),
        subject_id=r.subject_id,
        recording=deepcopy(r.recording),
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
        admission=None,
    ) -> None:
        self._admission = admission
        self._repo = repository
        self._engine = engine_port  # EnginePort | None
        self._command_results = command_results
        self._prepare_signal = None
        self._acquisition = None
        self._signal_lock = RLock()

    def set_signal_preparer(self, callback, lifecycle_lock):
        self._prepare_signal = callback
        self._signal_lock = lifecycle_lock

    def set_recording_controller(self, acquisition):
        """Bind raw recording to the session lifecycle at composition time."""
        self._acquisition = acquisition
        self._signal_lock = acquisition.lifecycle_lock

    @property
    def manages_recording_lifecycle(self) -> bool:
        return self._acquisition is not None

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @admitted
    def handle_create_session(self, cmd: CreateSessionCommand) -> str:
        with self._signal_lock:
            resume_stream = False
            if self._acquisition is not None:
                if cmd.continuation_of is not None:
                    if self._require(cmd.continuation_of).state != SessionRecordState.ENDED:
                        raise RuntimeError("A continuation requires an ended previous session")
                    if self.active_session_id is not None:
                        raise RuntimeError("Cannot resume while another session is active")
                resume_stream = self._acquisition.is_streaming
                active_id = self.active_session_id
                if active_id is not None:
                    self.handle_end_session(EndSessionCommand(active_id))
                else:
                    # Clear buffered samples even if a standalone stream was
                    # already stopped before this session was requested.
                    self._acquisition.handle_stop_and_drain()
            session_id = self._create_session(cmd)
            if self._acquisition is not None:
                self._begin_raw_recording(session_id)
                if resume_stream:
                    from omnibci.application.commands.acquisition_commands import StartStreamCommand
                    self._acquisition.handle_start_stream(StartStreamCommand())
            return session_id

    def _create_session(self, cmd: CreateSessionCommand) -> str:
        if cmd.continuation_of is not None:
            previous = self._require(cmd.continuation_of)
            if previous.state != SessionRecordState.ENDED:
                raise RuntimeError("A continuation requires an ended previous session")
            if self._repo.find_active() is not None:
                raise RuntimeError("Cannot resume while another session is active")
        config_revision = (
            cmd.config_revision if cmd.config_revision is not None
            else self._current_config_revision()
        )
        signal_plan = self._prepare_signal() if self._prepare_signal is not None else None
        session_id = str(uuid.uuid4())
        record = SessionRecord(
            session_id=session_id,
            paradigm=cmd.paradigm,
            state=SessionRecordState.RUNNING,
            started_at=datetime.now(timezone.utc),
            continuation_of=cmd.continuation_of,
            config_revision=config_revision,
            signal_plan=deepcopy(signal_plan),
            subject_id=cmd.subject_id,
        )
        self._repo.save(record)
        payload = {"session_id": session_id, "paradigm": cmd.paradigm}
        if cmd.subject_id is not None:
            payload["subject_id"] = cmd.subject_id
        if cmd.continuation_of is not None:
            payload["continuation_of"] = cmd.continuation_of
        if config_revision is not None:
            payload["config_revision"] = config_revision

        # Push the identity down into the signal-processing subprocess so its
        # BCISession aggregate uses the SAME id (see docs/session-id-unification.md).
        # On dispatch failure roll the record back — a session the engine never
        # heard of would just resurrect the old two-id split.
        if self._command_results is not None:
            try:
                self._command_results.request(
                    EventType.StartSession,
                    payload,
                )
            except CommandTimeoutError:
                # Timeout is an unknown outcome, not a confirmed failure: the
                # worker may have started this exact session and its late
                # SessionStarted event will reconcile the projection.
                if self._acquisition is not None:
                    # The source remains stopped. Arm recording even when the
                    # aggregate acknowledgement is late, so recovery cannot
                    # resume a session without its raw file.
                    self._begin_raw_recording(session_id)
                raise
            except BaseException:
                self._repo.delete(session_id)
                raise
        elif self._engine is not None:
            try:
                self._engine.dispatch(Event(
                    EventType.StartSession,
                    payload,
                ))
            except RuntimeError:
                self._repo.delete(session_id)
                raise

        logger.info("Session created: %s (paradigm=%s)", session_id, cmd.paradigm)
        return session_id

    @admitted
    def create_continuation(
        self, previous_id: str, *, config_revision: Optional[str] = None,
        paradigm: Optional[str] = None,
    ) -> str:
        """Start a fresh session linked to a closed session; never replay trials."""
        previous = self._require(previous_id)
        return self.handle_create_session(CreateSessionCommand(
            paradigm=previous.paradigm if paradigm is None else paradigm,
            continuation_of=previous_id,
            config_revision=config_revision,
            subject_id=previous.subject_id,
        ))

    @admitted(recovery=True)
    def handle_end_session(self, cmd: EndSessionCommand) -> None:
        with self._signal_lock:
            record = self._require(cmd.session_id)
            if self._acquisition is not None and record.state != SessionRecordState.ENDED:
                status = record.recording.get("status") if record.recording else None
                # A completed file already proves the drain and flush succeeded.
                # Retrying an EndSession acknowledgement must not drain an
                # aggregate that may have ended in the meantime.
                if status != "completed":
                    receipt = self._acquisition.handle_stop_and_drain(session_id=cmd.session_id)
                    trials = self._require(cmd.session_id).trials
                    if [trial.trial_number for trial in trials] != list(range(1, receipt.completed_trials + 1)):
                        raise RuntimeError("Session trial projection is incomplete; session retained for reconciliation")
                    if status == "interrupted":
                        cmd = EndSessionCommand(cmd.session_id, interruption_reason=(
                            cmd.interruption_reason or "Raw recording interrupted by source disconnect"
                        ))
                    elif record.recording is not None:
                        if status == "pending":
                            self._begin_raw_recording(cmd.session_id)
                        info = self._acquisition.end_raw_recording(cmd.session_id)
                        self._set_recording(cmd.session_id, info)
            self._end_aggregate(cmd)

    def _end_aggregate(self, cmd: EndSessionCommand) -> None:
        payload = cmd.session_id if cmd.interruption_reason is None else {
            "session_id": cmd.session_id,
            "interruption_reason": cmd.interruption_reason,
        }

        # Confirm the aggregate ended before the read model reports success.
        # SessionEnded is queued before CommandResult by the worker, so the
        # projection is normally already closed when request() returns; the
        # atomic mark below is an idempotent fallback.
        if self._command_results is not None:
            self._command_results.request(EventType.EndSession, payload)
        elif self._engine is not None:
            # Projection-only/legacy constructions retain best-effort dispatch.
            try:
                self._engine.dispatch(Event(EventType.EndSession, payload))
            except RuntimeError as exc:
                logger.warning("EndSession dispatch skipped (engine not ready): %s", exc)

        # Keep ending atomic with concurrent trial projection updates.
        record = self._repo.update(cmd.session_id, lambda r: r.mark_ended(
            interruption_reason=cmd.interruption_reason,
        ))
        if record is None:  # defensive: repository deletion raced the command
            raise KeyError(f"Session '{cmd.session_id}' not found")
        logger.info("Session ended: %s (%d trials)", cmd.session_id, record.trial_count)

    def _set_recording(self, session_id: str, info: dict) -> None:
        self._repo.update(session_id, lambda record: setattr(record, "recording", deepcopy(info)))

    def _begin_raw_recording(self, session_id: str) -> None:
        record = self._require(session_id)
        self._set_recording(session_id, {"session_id": session_id, "status": "pending"})
        try:
            info = self._acquisition.begin_raw_recording({
                "session_id": session_id, "subject_id": record.subject_id,
                "paradigm": record.paradigm, "continuation_of": record.continuation_of,
            })
        except CommandTimeoutError:
            # A late RawRecordingState event can still associate the file.
            raise
        except Exception:
            self._set_recording(session_id, {"session_id": session_id, "status": "failed"})
            try:
                info = self._acquisition.end_raw_recording(session_id)
            except Exception:
                logger.warning("Raw recording cleanup failed for session %s", session_id, exc_info=True)
            else:
                self._set_recording(session_id, info)
            self._end_aggregate(EndSessionCommand(
                session_id, interruption_reason="Raw recording could not be started",
            ))
            raise
        self._set_recording(session_id, {**info, "status": "recording"})

    def validate_recording_before_stream(self) -> None:
        record = self._repo.find_active()
        if record is not None and (record.recording is None or record.recording.get("status") != "recording"):
            raise RuntimeError("The active session has no writable raw recording; end it before streaming")

    def on_raw_recording_state(self, event: Event) -> None:
        """Reconcile late acknowledgements and interrupted worker shutdowns."""
        info = event.data
        if isinstance(info, dict) and isinstance(info.get("session_id"), str):
            self._set_recording(info["session_id"], info)

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_session(self, query: GetSessionQuery) -> SessionDTO:
        return _session_record_to_dto(self._require(query.session_id))

    def handle_list_sessions(self, query: ListSessionsQuery) -> List[SessionDTO]:
        return [_session_record_to_dto(r) for r in self._repo.find_all()]

    def export_snapshot(self, session_ids: Optional[List[str]] = None) -> List[SessionDTO]:
        """Read metadata and trials from one repository snapshot."""
        records = self._repo.find_all()
        if session_ids is not None:
            requested = set(session_ids)
            missing = requested - {record.session_id for record in records}
            if missing:
                raise KeyError(f"Session '{sorted(missing)[0]}' not found")
            records = [record for record in records if record.session_id in requested]
        return [_session_record_to_dto(record, include_trials=True) for record in records]

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
                config_revision=self._current_config_revision(),
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
                config_revision=(payload.config_revision if payload.config_revision is not None
                                 else record.config_revision),
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
                config_revision=payload.config_revision,
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
        record = self._repo.update(payload.session_id, lambda r: r.mark_ended(
            interruption_reason=payload.interruption_reason,
        ))
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

    def _current_config_revision(self) -> Optional[str]:
        snapshot = getattr(self._engine, "applied_snapshot", None)
        if isinstance(snapshot, dict):
            revision = snapshot.get("CONFIG.revision")
            return revision if isinstance(revision, str) else None
        return None

    def _require(self, session_id: str) -> SessionRecord:
        record = self._repo.find_by_id(session_id)
        if record is None:
            raise KeyError(f"Session '{session_id}' not found")
        return record
