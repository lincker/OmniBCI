"""Signal-stream state machine for epoch detection and decoding."""

import asyncio
from enum import Enum
import logging
from typing import Optional

from omnibci.domain.signal import SignalChunk

import numpy as np

from omnibci.domain.session import BCISession
from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.application.events import CommandRequest, CommandResult
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.event_translator import DomainEventPublisher
from .processing_state import AwaitingEpochState, AssemblingEpochState
from .epoch.epoch_processor import EpochProcessor
from .epoch.pretrigger_buffer import PreTriggerBuffer

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'
    # Session lifecycle (carry a payload — enqueued as (command, payload)):
    start_session = 'start_session'   # payload: {"session_id": str, "paradigm": str|None}
    end_session = 'end_session'       # payload: session_id str


class SignalProcessor:
    """
    Orchestrates the epoch-detection state machine.

    Owns:
      - EpochDetector  — reads start/end markers from each chunk
      - BCISession     — aggregate root for trial recording
      - State machine  — AwaitingEpochState / AssemblingEpochState
      - current_epoch  — the EpochProcessor for the epoch in flight (or None)

    Per-epoch lifecycle: a fresh EpochProcessor is created on each detected
    start (via new_epoch_processor()) and dropped on completion — no reused
    buffer / clear(). Its decoder comes from decoder.for_new_epoch(): stateless
    decoders share one instance (zero cost); stateful ones return a fresh
    per-epoch instance sharing the model. This is also the foundation for
    concurrent multi-buffer epochs (dense P300) later.
    """

    def __init__(
        self,
        detector: EpochDetector,
        decoder: Decoder,
        command_queue,
        data_manager,
        event_bus,
        session: Optional[BCISession] = None,
        sample_rate: int = 1000,
        baseline_duration: float = 0.0,
        filters=None,
        auto_start_session: bool = True,
    ) -> None:
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # ACL: the single place a domain-event translator is created. States
        # reach it via self.event_publisher — no import of this module from state.
        self.event_publisher = DomainEventPublisher(event_bus)

        # EpochDetector: reads epoch start/end markers from each chunk. Owned
        # here (the orchestration/state-machine level), not by EpochProcessor —
        # the state machine queries self.detector for boundaries.
        self.detector = detector

        # Ingredients for building a per-epoch EpochProcessor on demand.
        self._decoder = decoder
        # Ordered preprocessing filters (e.g. BaselineCorrection). Stateless pure
        # transforms, shared across all epochs.
        self._filters = list(filters or [])
        self._sample_rate = sample_rate
        # Baseline length is fixed for this processor's lifetime (a new paradigm
        # rebuilds the whole worker). Convert the paradigm's configured baseline
        # duration (seconds) into a sample count once, here.
        self.baseline_samples = round(baseline_duration * sample_rate)
        # Rolling pre-trigger store, fed by AwaitingEpochState; supplies the
        # baseline segment when a start marker fires. Capacity 0 = no baseline.
        self.pretrigger = PreTriggerBuffer(capacity=self.baseline_samples)
        # The EpochProcessor for the epoch currently being collected (or None
        # between epochs). Replaced — never reset — on each new start.
        self.current_epoch: Optional[EpochProcessor] = None

        # BCISession: created externally and injected so the process owner
        # controls session lifecycle. With session=None the behaviour depends
        # on auto_start_session:
        #   True  (default, tests/standalone): auto-create AND start — legacy.
        #   False (worker): create but DO NOT start — "standby": the session
        #          identity arrives later via a start_session command pushed
        #          down from the API (docs/session-id-unification.md). Trials
        #          decoded before that are dropped by record_trial's invariant.
        if session is None:
            session = BCISession()
            if auto_start_session:
                session.start()
        self.session: BCISession = session

        # States hold a reference to this processor (the context)
        self._awaiting_state = AwaitingEpochState(self)
        self._assembling_state = AssemblingEpochState(self)
        self._current_state = self._awaiting_state

        self._should_stop = False

    def new_epoch_processor(self) -> EpochProcessor:
        """Build a fresh EpochProcessor for a newly opened epoch."""
        return EpochProcessor(
            decoder=self._decoder.for_new_epoch(),
            sample_rate=self._sample_rate,
            filters=self._filters,
        )

    async def next_chunk(self, timeout: float = 0.05) -> "SignalChunk | None":
        """Wait off-loop for a chunk and return control after the timeout."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.data_manager.get_data, timeout)

    def baseline_from_chunk(self, chunk, start_idx: int) -> np.ndarray:
        """
        Baseline for an epoch that reopens mid-stream (back-to-back trials in
        AssemblingEpochState). The pre-trigger samples are the in-chunk slice
        just before *start_idx*, capped at ``baseline_samples``. When the gap
        between trials spans the chunk boundary the baseline is simply shorter —
        the ring buffer is only kept while idle (AwaitingEpochState).
        """
        if self.baseline_samples == 0:
            return chunk.data[:, :0]
        lo = max(0, start_idx - self.baseline_samples)
        return chunk.data[:, lo:start_idx]

    def run(self) -> None:
        asyncio.run(self.serve())

    def transition_to_awaiting(self) -> None:
        # Drop the just-completed epoch; the next start builds a fresh one.
        self.current_epoch = None
        self._current_state = self._awaiting_state
        logger.debug("→ AwaitingEpochState")

    def transition_to_assembling(self) -> None:
        self._current_state = self._assembling_state
        logger.debug("→ AssemblingEpochState")

    async def serve(self) -> None:
        while not self._should_stop:
            if not self.command_queue.empty():
                self._execute(self.command_queue.get())
            await self._current_state.run()

    def _execute(self, item) -> None:
        """
        Execute one queued command.

        Queue protocol: either a bare SignalProcessorCommand (legacy) or a
        ``(command, payload)`` tuple for commands that carry data — the engine's
        carry_data routes enqueue the latter (see NeuroEngine._command_routes).
        """
        if isinstance(item, tuple):
            command, raw_payload = item
        else:
            command, raw_payload = item, None
        request = CommandRequest.from_value(raw_payload)
        payload = request.payload if request is not None else raw_payload

        ok = True
        message = None
        error_code = None
        if command == SignalProcessorCommand.should_stop:
            self._should_stop = True
            # End the session cleanly so SessionEnded domain event is emitted
            self._end_current_session()
        elif command == SignalProcessorCommand.start_session:
            ok = self._start_session(payload)
            if not ok:
                message = "StartSession payload must contain a non-empty session_id"
                error_code = "INVALID_SESSION_COMMAND"
        elif command == SignalProcessorCommand.end_session:
            ok = self._end_session(payload)
            if not ok:
                message = (
                    f"EndSession id {payload!r} does not match the current session "
                    f"{self.session.session_id!r}"
                )
                error_code = "SESSION_ID_MISMATCH"
        else:
            logger.warning("SignalProcessor: unknown command %r", command)
            ok = False
            message = f"Unknown signal-processor command: {command!r}"
            error_code = "UNKNOWN_COMMAND"

        if request is not None:
            self._emit_command_result(
                request,
                command,
                ok=ok,
                message=message,
                error_code=error_code,
            )

    # ------------------------------------------------------------------
    # Session lifecycle (identity pushed down from the API — see
    # docs/session-id-unification.md)
    # ------------------------------------------------------------------

    def _start_session(self, payload) -> bool:
        """Open a session with the API-supplied identity (single-active invariant)."""
        if not isinstance(payload, dict) or not payload.get("session_id"):
            logger.error("start_session: bad payload %r; ignored", payload)
            return False
        # Single-active-session invariant: a new start ends the previous one.
        self._end_current_session()
        self.session = BCISession(
            session_id=payload["session_id"],
            paradigm=payload.get("paradigm"),
        )
        self.session.start()
        self.event_publisher.publish_all(self.session.pop_events())
        logger.info("Session started in worker: %s", self.session.session_id)
        return True

    def _end_session(self, payload) -> bool:
        """End the current session — only if the id matches (stale-command guard)."""
        session_id = payload if isinstance(payload, str) and payload else None
        if session_id is None:
            logger.error("end_session: bad payload %r; ignored", payload)
            return False
        if session_id != self.session.session_id:
            logger.warning("end_session for %s ignored (current is %s)",
                           session_id, self.session.session_id)
            return False
        self._end_current_session()
        return True

    def _end_current_session(self) -> None:
        """End + publish if RUNNING; silently no-op otherwise."""
        try:
            self.session.end()
        except RuntimeError:
            return  # already ended or never started
        self.event_publisher.publish_all(self.session.pop_events())

    def _emit_command_result(
        self,
        request: CommandRequest,
        command,
        *,
        ok: bool,
        message: str | None,
        error_code: str | None,
    ) -> None:
        command_name = {
            SignalProcessorCommand.start_session: EventType.StartSession.name,
            SignalProcessorCommand.end_session: EventType.EndSession.name,
        }.get(command, command.name if isinstance(command, Enum) else str(command))
        self.event_bus.put_event(Event(
            EventType.CommandResult,
            CommandResult(
                command_id=request.command_id,
                command=command_name,
                ok=ok,
                message=message,
                error_code=error_code,
            ),
        ))
