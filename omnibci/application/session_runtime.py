"""Own the active BCI aggregate inside the signal-processing worker process."""

from collections.abc import Callable, Iterable
import logging

from omnibci.application.events import StreamDrainReceipt
from omnibci.application.processing_session import DecodedTrial
from omnibci.domain.events import DomainEvent, TrialCompleted
from omnibci.domain.session import BCISession, SessionState

logger = logging.getLogger(__name__)


class SessionRuntime:
    """Apply session rules and publish each result before processing continues."""

    def __init__(self, publish_events: Callable[[Iterable[DomainEvent]], None], *,
                 session: BCISession | None = None, auto_start_session: bool = False,
                 config_revision: str | None = None) -> None:
        self._publish_events = publish_events
        if session is None:
            session = BCISession(config_revision=config_revision)
            if auto_start_session:
                session.start()
        self.session = session
        self._closed_session_ids = (
            {session.session_id} if session.state is SessionState.ENDED else set()
        )

    def record_trial(self, result: DecodedTrial) -> TrialCompleted | None:
        if self.session.state is not SessionState.RUNNING:
            logger.warning("Decoded trial discarded because the session is %s", self.session.state.name)
            return None
        completion = self.session.record_trial(
            result.epoch_info, result.classification, config_revision=result.config_revision,
        )
        self._publish_events(self.session.pop_events())
        return completion

    def start_session(self, payload, *, config_revision: str | None,
                      before_end: Callable[[str], None]) -> bool:
        if (not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str)
                or not payload["session_id"]):
            logger.error("start_session: bad payload %r; ignored", payload)
            return False
        session_id = payload["session_id"]
        if session_id in self._closed_session_ids:
            logger.warning("start_session: ended session %s cannot be reused", session_id)
            return False
        if session_id == self.session.session_id and self.session.state is SessionState.RUNNING:
            return all(
                field not in payload or payload[field] == getattr(self.session, field)
                for field in ("paradigm", "continuation_of", "config_revision", "subject_id")
            )
        try:
            candidate = BCISession(
                session_id=session_id, paradigm=payload.get("paradigm"),
                continuation_of=payload.get("continuation_of"),
                config_revision=payload.get("config_revision", config_revision),
                subject_id=payload.get("subject_id"),
            )
        except ValueError as exc:
            logger.error("start_session: %s", exc)
            return False
        self.end_current_session(before_end=before_end)
        self.session = candidate
        self.session.start()
        self._publish_events(self.session.pop_events())
        logger.info("Session started in worker: %s", self.session.session_id)
        return True

    def end_session(self, payload, *, before_end: Callable[[str], None]) -> bool:
        session_id = payload.get("session_id") if isinstance(payload, dict) else payload
        reason = payload.get("interruption_reason") if isinstance(payload, dict) else None
        if (not isinstance(session_id, str) or not session_id
                or (reason is not None and not isinstance(reason, str))):
            logger.error("end_session: bad payload %r; ignored", payload)
            return False
        if session_id != self.session.session_id:
            logger.warning("end_session for %s ignored (current is %s)", session_id, self.session.session_id)
            return False
        self.end_current_session(before_end=before_end, interruption_reason=reason)
        return True

    def end_current_session(self, *, before_end: Callable[[str], None],
                            interruption_reason: str | None = None) -> None:
        before_end("Session ended before the trial boundary")
        if self.session.state is not SessionState.RUNNING:
            return
        self.session.end(interruption_reason=interruption_reason)
        self._closed_session_ids.add(self.session.session_id)
        self._publish_events(self.session.pop_events())

    def validate_drain(self, session_id: str | None) -> str | None:
        running = self.session.session_id if self.session.state is SessionState.RUNNING else None
        if session_id is not None and session_id != running:
            raise ValueError("Drain barrier session differs from the running session")
        return running

    def drain_receipt(self, *, generation: str | None, config_revision: str | None,
                      last_sequence: int, interrupted_epoch: bool) -> StreamDrainReceipt:
        return StreamDrainReceipt(
            generation, config_revision, last_sequence, self.validate_drain(None),
            self.session.trial_count, interrupted_epoch,
        )
