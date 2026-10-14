"""Signal-stream state machine for epoch detection and decoding."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from enum import Enum
import hashlib
import json
import logging
from typing import Optional

from omnibci.domain.signal import SignalChunk

import numpy as np

from omnibci.domain.session import BCISession, SessionState
from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.application.events import (
    CommandFailureKind, CommandRequest, CommandResult, StreamBarrier, StreamDrainReceipt,
)
from omnibci.application.processing_source import requires_signal_preparation, validate_plan_binding
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.event_translator import DomainEventPublisher
from .processing_state import AwaitingEpochState, AssemblingEpochState
from .epoch.epoch_processor import EpochProcessor
from .epoch.pretrigger_buffer import PreTriggerBuffer
from .pipeline_configuration import build_pipeline_update

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'
    # Session lifecycle (carry a payload — enqueued as (command, payload)):
    start_session = 'start_session'   # payload: {"session_id": str, "paradigm": str|None}
    end_session = 'end_session'       # payload: session_id str
    stage_configuration = 'stage_processing_configuration'
    cancel_configuration = 'cancel_processing_configuration'
    update_context = 'update_processing_context'
    prepare_signal = 'prepare_signal_processing'


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
        generation: str | None = None,
        config_revision: str | None = None,
        settings_snapshot: dict | None = None,
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
        self._baseline_duration = baseline_duration
        self._settings_snapshot = deepcopy(settings_snapshot)
        self._signal_plan = None
        self._signal_receipt = None
        self._pending_configuration = None
        # Command receipts live for this worker generation. A cancelled command
        # cannot become a new update if a delayed duplicate arrives later.
        self._configuration_results = {}
        self._configuration_fingerprints = {}
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
            session = BCISession(config_revision=config_revision)
            if auto_start_session:
                session.start()
        self.session: BCISession = session
        self._closed_session_ids = (
            {session.session_id} if session.state is SessionState.ENDED else set()
        )

        # States hold a reference to this processor (the context)
        self._awaiting_state = AwaitingEpochState(self)
        self._assembling_state = AssemblingEpochState(self)
        self._current_state = self._awaiting_state

        self._should_stop = False
        self.generation = generation
        self.config_revision = config_revision
        self._last_sequence = -1
        self._drain_failure: str | None = None

    def new_epoch_processor(self) -> EpochProcessor:
        """Build a fresh EpochProcessor for a newly opened epoch."""
        self._install_pending_configuration()
        return EpochProcessor(
            decoder=self._decoder.for_new_epoch(),
            sample_rate=self._sample_rate,
            filters=self._filters,
            config_revision=self.config_revision,
        )

    async def next_chunk(self, timeout: float = 0.05) -> "SignalChunk | None":
        """Wait off-loop for a chunk and return control after the timeout."""
        loop = asyncio.get_running_loop()
        pending = loop.run_in_executor(None, self.data_manager.get_data, timeout)
        try:
            item = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A cancelled await does not cancel a blocking queue get. Join it
            # before shutdown so an already-consumed barrier is not orphaned.
            item = await pending
            if isinstance(item, StreamBarrier):
                self._emit_drain_result(item, ok=False, error_code="PROCESSOR_CANCELLED",
                                        message="Processing stopped before completing the drain")
            raise
        if isinstance(item, StreamBarrier):
            self._on_stream_barrier(item)
            return None
        if isinstance(item, SignalChunk):
            if requires_signal_preparation(self._settings_snapshot or {}):
                if self._signal_plan is None:
                    raise RuntimeError("Signal processing has not been prepared")
                if self._signal_plan.requires_data_mapping:
                    item = self._signal_plan.apply(item)
            self._last_sequence = item.sequence_id
        return item

    def _on_stream_barrier(self, barrier: StreamBarrier) -> None:
        """Prior data and its trial events have completed on this producer."""
        processing_started = False
        try:
            if barrier.generation != self.generation or barrier.config_revision != self.config_revision:
                raise ValueError("Drain barrier belongs to a different runtime generation or revision")
            if barrier.last_sequence != self._last_sequence:
                raise ValueError("Drain barrier sequence differs from the last consumed signal chunk")
            running_session = self.session.session_id if self.session.state is SessionState.RUNNING else None
            if barrier.session_id is not None and barrier.session_id != running_session:
                raise ValueError("Drain barrier session differs from the running session")
            if self._drain_failure is not None:
                raise RuntimeError(self._drain_failure)
            processing_started = True
            interrupted = self.current_epoch is not None
            if self.current_epoch is not None:
                complete, classification = self.current_epoch.recognize_if_ready()
                if complete and classification is not None:
                    previous_count = self.session.trial_count
                    self._assembling_state._complete_trial(classification)
                    if running_session is not None and self.session.trial_count != previous_count + 1:
                        raise RuntimeError("Completed drain trial could not be recorded")
                    interrupted = False
            self.transition_to_awaiting()
            self.pretrigger.clear()
            receipt = StreamDrainReceipt(
                self.generation, self.config_revision, self._last_sequence,
                running_session, self.session.trial_count, interrupted,
            )
            self._emit_drain_result(barrier, ok=True, data=receipt.to_dict())
        except Exception as exc:
            if processing_started:
                # An incremental decoder or trial publisher may already have
                # mutated state. A retry must not silently discard that failure.
                self._drain_failure = str(exc)
            logger.exception("Stream drain failed: %s", exc)
            self._emit_drain_result(barrier, ok=False, error_code="STREAM_DRAIN_FAILED", message=str(exc))

    def _emit_drain_result(self, barrier: StreamBarrier, *, ok: bool, data=None,
                           error_code=None, message=None) -> None:
        self.event_bus.put_event(Event(EventType.CommandResult, CommandResult(
            command_id=barrier.command_id, command=EventType.StopAndDrain.name,
            ok=ok, message=message, error_code=error_code, data=data,
            failure_kind=None if ok else CommandFailureKind.EXECUTION_FAILED,
        )))

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
        self._install_pending_configuration()
        logger.debug("→ AwaitingEpochState")

    def transition_to_assembling(self) -> None:
        self._current_state = self._assembling_state
        logger.debug("→ AssemblingEpochState")

    async def serve(self) -> None:
        try:
            while not self._should_stop:
                if not self.command_queue.empty():
                    self._execute(self.command_queue.get())
                if self._should_stop:
                    break
                await self._current_state.run()
        finally:
            self._cancel_pending_configuration("Processing stopped before the trial boundary")

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
        if command == SignalProcessorCommand.prepare_signal:
            if request is not None:
                self._prepare_signal(request)
            return
        if command == SignalProcessorCommand.update_context:
            if request is not None:
                self._update_output_context(request)
            return
        if command in (SignalProcessorCommand.stage_configuration, SignalProcessorCommand.cancel_configuration):
            if request is None:
                logger.warning("Processing configuration command requires a correlation envelope")
                return
            if command == SignalProcessorCommand.stage_configuration:
                self._stage_configuration(request)
            else:
                self._cancel_configuration(request)
            return

        ok = True
        message = None
        error_code = None
        if command == SignalProcessorCommand.should_stop:
            self._should_stop = True
            # End the session cleanly so SessionEnded domain event is emitted
            self._end_current_session()
        elif command == SignalProcessorCommand.start_session:
            ok = (not requires_signal_preparation(self._settings_snapshot or {})
                  or self._signal_receipt is not None) and self._start_session(payload)
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

    def _prepare_signal(self, request):
        from omnibci.domain.channel_mapping import ResolvedSignalPlan
        from .signal_configuration import load_signal_pipeline
        try:
            payload = request.payload
            if not isinstance(payload, dict) or set(payload) != {"plan", "receipt"}:
                raise ValueError("Invalid signal preparation request")
            plan = ResolvedSignalPlan.from_dict(payload["plan"])
            receipt = payload["receipt"]
            if (not isinstance(receipt, dict)
                    or set(receipt) != {"generation", "config_revision", "connection_id",
                                       "source_fingerprint", "plan_fingerprint"}
                    or receipt["generation"] != self.generation
                    or receipt["config_revision"] != self.config_revision
                    or receipt["connection_id"] != plan.source.connection_id
                    or not receipt["connection_id"]
                    or receipt["source_fingerprint"] != plan.source.fingerprint
                    or receipt["plan_fingerprint"] != plan.fingerprint
                    or plan.config_revision != self.config_revision):
                raise ValueError("Signal preparation belongs to a different runtime or connection")
            validate_plan_binding(self._settings_snapshot or {}, plan)
            if receipt != self._signal_receipt:
                if self.current_epoch is not None or self._pending_configuration is not None:
                    raise ValueError("Drain the stream before preparing a new signal plan")
                decoder, detector, filters = load_signal_pipeline(self._settings_snapshot, plan)
                self._decoder, self.detector, self._filters = decoder, detector, list(filters)
                self._sample_rate = plan.source.sample_rate
                self.baseline_samples = round(self._baseline_duration * self._sample_rate)
                self.pretrigger = PreTriggerBuffer(capacity=self.baseline_samples)
                self._signal_plan = plan
                self._signal_receipt = deepcopy(receipt)
            result = CommandResult(request.command_id, EventType.PrepareSignalProcessing.name,
                                   ok=True, data=deepcopy(receipt))
        except Exception as exc:
            result = CommandResult(request.command_id, EventType.PrepareSignalProcessing.name,
                                   ok=False, message=str(exc), error_code="SIGNAL_PREPARATION_FAILED",
                                   failure_kind=CommandFailureKind.REJECTED)
        self._publish_configuration_result(result)

    def _start_session(self, payload) -> bool:
        """Open a session with the API-supplied identity (single-active invariant)."""
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("session_id"), str)
            or not payload["session_id"]
        ):
            logger.error("start_session: bad payload %r; ignored", payload)
            return False
        session_id = payload["session_id"]
        if session_id in self._closed_session_ids:
            logger.warning("start_session: ended session %s cannot be reused", session_id)
            return False
        if session_id == self.session.session_id and self.session.state is SessionState.RUNNING:
            # Retrying a confirmed identity must not reset its trials or events.
            return all(
                field not in payload or payload[field] == getattr(self.session, field)
                for field in ("paradigm", "continuation_of", "config_revision")
            )
        candidate = BCISession(
            session_id=session_id,
            paradigm=payload.get("paradigm"),
            continuation_of=payload.get("continuation_of"),
            config_revision=payload.get("config_revision", self.config_revision),
        )
        # Single-active-session invariant: a new start ends the previous one.
        self._end_current_session()
        self.session = candidate
        self.session.start()
        self.event_publisher.publish_all(self.session.pop_events())
        logger.info("Session started in worker: %s", self.session.session_id)
        return True

    def _end_session(self, payload) -> bool:
        """End the current session — only if the id matches (stale-command guard)."""
        session_id = payload.get("session_id") if isinstance(payload, dict) else payload
        reason = payload.get("interruption_reason") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id or (reason is not None and not isinstance(reason, str)):
            logger.error("end_session: bad payload %r; ignored", payload)
            return False
        if session_id != self.session.session_id:
            logger.warning("end_session for %s ignored (current is %s)",
                           session_id, self.session.session_id)
            return False
        self._end_current_session(interruption_reason=reason)
        return True

    def _end_current_session(self, *, interruption_reason: str | None = None) -> None:
        """End + publish if RUNNING; silently no-op otherwise."""
        self._cancel_pending_configuration("Session ended before the trial boundary")
        try:
            self.session.end(interruption_reason=interruption_reason)
        except RuntimeError:
            return  # already ended or never started
        self._closed_session_ids.add(self.session.session_id)
        self.event_publisher.publish_all(self.session.pop_events())

    def _configuration_receipt(self, command_id, status):
        return {"status": status, "command_id": command_id,
                "applied_revision": self.config_revision, "generation": self.generation}

    def _update_output_context(self, request):
        from .pipeline_configuration import validate_output_context
        try:
            payload = request.payload
            if (not isinstance(payload, dict) or payload.get("generation") != self.generation
                    or payload.get("expected_revision") != self.config_revision):
                raise ValueError("Runtime context command is stale")
            if self.current_epoch is not None or self._pending_configuration is not None:
                raise ValueError("Drain processing before updating output context")
            snapshot = validate_output_context(self._settings_snapshot, payload.get("settings_snapshot"))
            self._settings_snapshot = snapshot
            self.config_revision = snapshot["CONFIG.revision"]
            if self._signal_plan is not None:
                self._signal_plan = replace(self._signal_plan, config_revision=self.config_revision)
            if self._signal_receipt is not None:
                self._signal_receipt["config_revision"] = self.config_revision
            result = CommandResult(request.command_id, EventType.UpdateProcessingContext.name, ok=True,
                                   data=self._configuration_receipt(request.command_id, "applied"))
        except Exception as exc:
            result = CommandResult(request.command_id, EventType.UpdateProcessingContext.name, ok=False,
                                   message=str(exc), error_code="PROCESSING_CONTEXT_FAILED",
                                   failure_kind=CommandFailureKind.REJECTED)
        self._publish_configuration_result(result)

    def _publish_configuration_result(self, result):
        self.event_bus.put_event(Event(EventType.CommandResult, result))

    def _finish_configuration(self, command_id, *, ok, message=None, error_code=None):
        result = CommandResult(
            command_id=command_id, command=EventType.StageProcessingConfiguration.name,
            ok=ok, message=message, error_code=error_code,
            failure_kind=None if ok else CommandFailureKind.REJECTED,
            data=self._configuration_receipt(command_id, "applied" if ok else "cancelled"),
        )
        self._configuration_results[command_id] = result
        self._publish_configuration_result(result)

    def _stage_configuration(self, request):
        try:
            completed = self._configuration_results.get(request.command_id)
            if completed is not None:
                self._publish_configuration_result(completed)
                return
            fingerprint = hashlib.sha256(json.dumps(
                request.payload, sort_keys=True, allow_nan=False, separators=(",", ":"),
            ).encode()).hexdigest()
            previous = self._configuration_fingerprints.get(request.command_id)
            if previous is not None and previous != fingerprint:
                if self._pending_configuration is not None and self._pending_configuration[0] == request.command_id:
                    self._cancel_pending_configuration("A processing configuration command id was reused")
                    return
                raise ValueError("A processing configuration command id cannot be reused")
            if self._pending_configuration is not None:
                if self._pending_configuration[0] == request.command_id:
                    return
                raise ValueError("Another processing configuration is waiting for a trial boundary")
            self._configuration_fingerprints[request.command_id] = fingerprint
            payload = request.payload
            if not isinstance(payload, dict) or set(payload) != {"settings_snapshot", "expected_revision", "generation"}:
                raise ValueError("Invalid processing configuration request")
            if (not isinstance(payload["generation"], str) or not payload["generation"]
                    or payload["generation"] != self.generation):
                raise ValueError("Processing configuration generation mismatch")
            if (not isinstance(payload["expected_revision"], str)
                    or payload["expected_revision"] != self.config_revision):
                raise ValueError("Processing configuration revision mismatch")
            candidate = build_pipeline_update(
                self._settings_snapshot, payload["settings_snapshot"],
                sample_rate=self._sample_rate, baseline_duration=self._baseline_duration,
                signal_plan=self._signal_plan,
            )
        except Exception as exc:
            # Rejecting a conflicting duplicate must not overwrite the receipt
            # or pending update belonging to the original command.
            result = CommandResult(
                command_id=request.command_id, command=EventType.StageProcessingConfiguration.name,
                ok=False, message=str(exc), error_code="PROCESSING_CONFIGURATION_REJECTED",
                failure_kind=CommandFailureKind.REJECTED,
            )
            if request.command_id not in self._configuration_results and (
                self._pending_configuration is None or self._pending_configuration[0] != request.command_id
            ):
                self._configuration_results[request.command_id] = result
            self._publish_configuration_result(result)
            return
        self._pending_configuration = (request.command_id, candidate)
        if self.current_epoch is None:
            self._install_pending_configuration()

    def _install_pending_configuration(self):
        if self._pending_configuration is None:
            return
        command_id, candidate = self._pending_configuration
        self._decoder = candidate.decoder
        self._filters = list(candidate.filters)
        self._settings_snapshot = candidate.settings_snapshot
        self.config_revision = candidate.settings_snapshot["CONFIG.revision"]
        if self._signal_plan is not None:
            self._signal_plan = replace(self._signal_plan, config_revision=self.config_revision)
        if self._signal_receipt is not None:
            self._signal_receipt["config_revision"] = self.config_revision
        self._pending_configuration = None
        self._finish_configuration(command_id, ok=True)

    def _cancel_pending_configuration(self, message):
        if self._pending_configuration is None:
            return
        command_id, _ = self._pending_configuration
        self._pending_configuration = None
        self._finish_configuration(command_id, ok=False, message=message,
                                   error_code="PROCESSING_CONFIGURATION_CANCELLED")

    def _cancel_configuration(self, request):
        payload = request.payload
        if (not isinstance(payload, dict) or set(payload) != {"command_id", "generation"}
                or not isinstance(payload.get("command_id"), str) or not payload["command_id"]
                or not isinstance(payload.get("generation"), str) or not payload["generation"]
                or payload.get("generation") != self.generation):
            self._publish_configuration_result(CommandResult(
                request.command_id, EventType.CancelProcessingConfiguration.name, False,
                "Invalid processing configuration cancellation", "INVALID_CONFIGURATION_CANCELLATION",
                CommandFailureKind.REJECTED,
            ))
            return
        target = payload["command_id"]
        if self._pending_configuration is not None and self._pending_configuration[0] == target:
            self._cancel_pending_configuration("Cancelled before the trial boundary")
        result = self._configuration_results.get(target)
        if result is None:
            # A cancellation received before a delayed stage command is a
            # tombstone: that command must never install later in this run.
            self._finish_configuration(target, ok=False, message="Cancelled before staging",
                                       error_code="PROCESSING_CONFIGURATION_CANCELLED")
            result = self._configuration_results[target]
        receipt = result.data or self._configuration_receipt(target, "cancelled")
        self._publish_configuration_result(CommandResult(
            request.command_id, EventType.CancelProcessingConfiguration.name, True, data=receipt,
        ))

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
