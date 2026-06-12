"""
RecordingService — application-layer orchestrator for "record a run".

The Acquisition and Session bounded contexts stay independent (each keeps its
own AppService and is still individually controllable via the primitive
endpoints). This service names the *use case* that spans both — "start/stop a
recording" — and owns the cross-context ordering so neither context has to know
about the other (no event-level coupling, no context reaching into another).

Ordering rules (see docs/frontend-separation.md / session discussion):
  start():  create session FIRST (must be RUNNING before data flows, else the
            first epochs decode with no session and their trials are dropped),
            THEN start the stream. If stream start fails, end the just-created
            session so we don't leave an orphan RUNNING record.
  stop():   stop the stream FIRST (no new epochs), wait a short drain window so
            in-flight epochs already in the processor queue still record, THEN
            end the session.

This keeps the two primitives (POST /sessions, /acquisition/stream/*) available
for advanced flows (calibration without recording, several sessions within one
continuous stream); RecordingService is the convenience path for the common
"one stream = one recording" case.
"""

import logging
import time
from typing import Optional

from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.commands.session_commands import (
    CreateSessionCommand,
    EndSessionCommand,
)
from omnibci.application.commands.acquisition_commands import (
    StartStreamCommand,
    StopStreamCommand,
)

logger = logging.getLogger(__name__)


class RecordingConflictError(RuntimeError):
    """Raised by start() when a recording is already active."""


class NoActiveRecordingError(RuntimeError):
    """Raised by stop() when there is no active recording."""


class RecordingService:
    """Coordinates Session + Acquisition for the 'record a run' use case."""

    def __init__(
        self,
        session_service: SessionAppService,
        acquisition_service: AcquisitionAppService,
        stop_drain_seconds: float = 0.3,
    ) -> None:
        self._sessions = session_service
        self._acquisition = acquisition_service
        # Time to let in-flight epochs drain from the processor queue between
        # stopping the stream and ending the session. Configurable; the residual
        # race (an epoch still mid-decode after the window) is bounded and only
        # ever loses the very last trial.
        self._stop_drain_seconds = stop_drain_seconds

    def start(self, paradigm: Optional[str] = None) -> str:
        """
        Begin a recording: create + start a session, then start the stream.

        Returns the new session_id. Raises:
          RecordingConflictError — a recording is already active.
          RuntimeError           — engine not ready (propagated from services);
                                    the half-created session is rolled back.
        """
        if self._sessions.active_session_id is not None:
            raise RecordingConflictError(
                "A recording is already active; stop it before starting another."
            )

        # 1. Session first — must be RUNNING before data flows.
        session_id = self._sessions.handle_create_session(
            CreateSessionCommand(paradigm=paradigm)
        )

        # 2. Stream second; on failure roll the session back to avoid an orphan.
        try:
            self._acquisition.handle_start_stream(StartStreamCommand())
        except Exception:
            logger.exception("Stream start failed; rolling back session %s", session_id)
            try:
                self._sessions.handle_end_session(EndSessionCommand(session_id=session_id))
            except Exception:  # pragma: no cover - defensive
                logger.exception("Rollback end_session failed for %s", session_id)
            raise

        logger.info("Recording started: session=%s paradigm=%s", session_id, paradigm)
        return session_id

    def stop(self) -> str:
        """
        End the active recording: stop the stream, drain, then end the session.

        Returns the ended session_id. Raises NoActiveRecordingError if none.
        """
        session_id = self._sessions.active_session_id
        if session_id is None:
            raise NoActiveRecordingError("No active recording to stop.")

        # 1. Stop the stream first — no new epochs after this.
        self._acquisition.handle_stop_stream(StopStreamCommand())

        # 2. Let in-flight epochs already queued in the processor finish recording.
        if self._stop_drain_seconds > 0:
            time.sleep(self._stop_drain_seconds)

        # 3. End the session.
        self._sessions.handle_end_session(EndSessionCommand(session_id=session_id))
        logger.info("Recording stopped: session=%s", session_id)
        return session_id

    @property
    def active_session_id(self) -> Optional[str]:
        return self._sessions.active_session_id
