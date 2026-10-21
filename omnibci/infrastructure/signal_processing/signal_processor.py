"""Signal-stream state machine for epoch detection and decoding."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from enum import Enum
import logging
from queue import Empty
from typing import Optional
from uuid import uuid4

from omnibci.domain.signal import SignalChunk

from omnibci.domain.classification import Classification
from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.application.events import (
    CommandFailureKind, CommandRequest, CommandResult, StreamBarrier, StreamDrainReceipt, TrialOutcome,
)
from omnibci.application.processing_source import requires_signal_preparation, validate_plan_binding
from omnibci.shared.events import Event, EventType
from .processing_state import AwaitingEpochState, AssemblingEpochState
from .epoch.epoch_processor import EpochProcessor
from .epoch.pretrigger_buffer import PreTriggerBuffer
from .configuration_transactions import ConfigurationTransactions, configuration_receipt

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'
    stage_configuration = 'stage_processing_configuration'
    cancel_configuration = 'cancel_processing_configuration'
    update_context = 'update_processing_context'
    prepare_signal = 'prepare_signal_processing'


class SignalProcessor:
    """
    Orchestrates the epoch-detection state machine.

    Owns:
      - EpochDetector  — reads start/end markers from each chunk
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
        sample_rate: int | None = None,
        baseline_duration: float = 0.0,
        filters=None,
        generation: str | None = None,
        config_revision: str | None = None,
        settings_snapshot: dict | None = None,
    ) -> None:
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue

        # EpochDetector: reads epoch start/end markers from each chunk. Owned
        # here (the orchestration/state-machine level), not by EpochProcessor —
        # the state machine queries self.detector for boundaries.
        self.detector = detector

        # Ingredients for building a per-epoch EpochProcessor on demand.
        self._decoder = decoder
        # Ordered preprocessing filters (e.g. BaselineCorrection). Stateless pure
        # transforms, shared across all epochs.
        self._filters = list(filters or [])
        self._sample_rate = (None if requires_signal_preparation(settings_snapshot or {}) else sample_rate)
        self._baseline_duration = baseline_duration
        self._settings_snapshot = deepcopy(settings_snapshot)
        self._signal_plan = None
        self._signal_receipt = None
        self._configuration_transactions = ConfigurationTransactions()
        # The legacy baseline_duration setting controls pre-trigger capture,
        # independently of whether a baseline-correction filter is configured.
        # Signal preparation supplies the observed sample rate.
        self.pre_trigger_samples = (round(baseline_duration * self._sample_rate)
                                    if self._sample_rate is not None else 0)
        # Rolling history of the continuous signal; supplies the samples before
        # each start marker, including prior trials. Capacity 0 disables capture.
        self.pretrigger = PreTriggerBuffer(capacity=self.pre_trigger_samples)
        # The EpochProcessor for the epoch currently being collected (or None
        # between epochs). Replaced — never reset — on each new start.
        self.current_epoch: Optional[EpochProcessor] = None

        # States hold a reference to this processor (the context)
        self._awaiting_state = AwaitingEpochState(self)
        self._assembling_state = AssemblingEpochState(self)
        self._current_state = self._awaiting_state

        self._should_stop = False
        self.generation = generation or (settings_snapshot or {}).get("CONFIG.generation") or str(uuid4())
        self.completed_trials = 0
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

    def complete_trial(self, classification: Classification) -> TrialOutcome:
        """Publish each decoded result before advancing to the next epoch."""
        outcome = TrialOutcome(
            generation=self.generation, trial_number=self.completed_trials + 1,
            classification=classification,
            config_revision=self.current_epoch.config_revision,
        )
        self.event_bus.put_event(Event(EventType.TrialCompleted, outcome))
        self.completed_trials = outcome.trial_number
        return outcome

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
                                        message="Signal Processing stopped before completing the drain")
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
            if self._drain_failure is not None:
                raise RuntimeError(self._drain_failure)
            processing_started = True
            interrupted = self.current_epoch is not None
            if self.current_epoch is not None:
                complete, classification = self.current_epoch.decode_if_ready()
                if complete and classification is not None:
                    self.complete_trial(classification)
                    interrupted = False
            self.transition_to_awaiting()
            self.pretrigger.clear()
            receipt = StreamDrainReceipt(
                generation=self.generation, config_revision=self.config_revision,
                last_sequence=self._last_sequence, completed_trials=self.completed_trials,
                interrupted_epoch=interrupted,
            )
            self._emit_drain_result(barrier, ok=True, data=receipt.to_dict())
        except Exception as exc:
            if processing_started:
                # A stateful decoder or trial publisher may already have
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
                try:
                    command = self.command_queue.get_nowait()
                except Empty:
                    pass
                else:
                    self._execute(command)
                if self._should_stop:
                    break
                await self._current_state.run()
        finally:
            self._cancel_pending_configuration("Signal Processing stopped before the trial boundary")

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
                logger.warning("Signal Processing configuration command requires a correlation envelope")
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
            self._cancel_pending_configuration("Signal Processing stopped before the trial boundary")
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
                if self.current_epoch is not None or self._configuration_transactions.pending is not None:
                    raise ValueError("Drain the stream before preparing a new signal plan")
                decoder, detector, filters = load_signal_pipeline(self._settings_snapshot, plan)
                self._decoder, self.detector, self._filters = decoder, detector, list(filters)
                self._sample_rate = plan.source.sample_rate
                self.pre_trigger_samples = round(self._baseline_duration * self._sample_rate)
                self.pretrigger = PreTriggerBuffer(capacity=self.pre_trigger_samples)
                self._signal_plan = plan
                self._signal_receipt = deepcopy(receipt)
            result = CommandResult(request.command_id, EventType.PrepareSignalProcessing.name,
                                   ok=True, data=deepcopy(receipt))
        except Exception as exc:
            result = CommandResult(request.command_id, EventType.PrepareSignalProcessing.name,
                                   ok=False, message=str(exc), error_code="SIGNAL_PREPARATION_FAILED",
                                   failure_kind=CommandFailureKind.REJECTED)
        self._publish_configuration_result(result)

    def _update_output_context(self, request):
        from .pipeline_configuration import validate_output_context
        try:
            payload = request.payload
            if (not isinstance(payload, dict) or payload.get("generation") != self.generation
                    or payload.get("expected_revision") != self.config_revision):
                raise ValueError("Runtime context command is stale")
            if self.current_epoch is not None or self._configuration_transactions.pending is not None:
                raise ValueError("Drain processing before updating output context")
            snapshot = validate_output_context(self._settings_snapshot, payload.get("settings_snapshot"))
            self._install_configuration(snapshot)
            result = CommandResult(request.command_id, EventType.UpdateProcessingContext.name, ok=True,
                                   data=configuration_receipt(request.command_id, "applied",
                                                              generation=self.generation, revision=self.config_revision))
        except Exception as exc:
            result = CommandResult(request.command_id, EventType.UpdateProcessingContext.name, ok=False,
                                   message=str(exc), error_code="PROCESSING_CONTEXT_FAILED",
                                   failure_kind=CommandFailureKind.REJECTED)
        self._publish_configuration_result(result)

    def _publish_configuration_result(self, result):
        self.event_bus.put_event(Event(EventType.CommandResult, result))

    def _stage_configuration(self, request):
        newly_staged, result = self._configuration_transactions.stage(
            request, generation=self.generation, revision=self.config_revision,
            current_snapshot=self._settings_snapshot, sample_rate=self._sample_rate,
            baseline_duration=self._baseline_duration, signal_plan=self._signal_plan,
        )
        if result is not None:
            self._publish_configuration_result(result)
        if newly_staged and self.current_epoch is None:
            self._install_pending_configuration()

    def _install_configuration(self, snapshot, pipeline=None):
        if pipeline is not None:
            self._decoder = pipeline.decoder
            self._filters = list(pipeline.filters)
        self._settings_snapshot = snapshot
        self.config_revision = snapshot["CONFIG.revision"]
        if self._signal_plan is not None:
            self._signal_plan = replace(self._signal_plan, config_revision=self.config_revision)
        if self._signal_receipt is not None:
            self._signal_receipt["config_revision"] = self.config_revision

    def _install_pending_configuration(self):
        pending = self._configuration_transactions.pending
        if pending is None:
            return
        _, candidate = pending
        self._install_configuration(candidate.settings_snapshot, pipeline=candidate)
        result = self._configuration_transactions.complete_applied(
            generation=self.generation, revision=self.config_revision,
        )
        self._publish_configuration_result(result)

    def _cancel_pending_configuration(self, message):
        result = self._configuration_transactions.cancel_pending(
            message, generation=self.generation, revision=self.config_revision,
        )
        if result is not None:
            self._publish_configuration_result(result)

    def _cancel_configuration(self, request):
        for result in self._configuration_transactions.cancel(
            request, generation=self.generation, revision=self.config_revision,
        ):
            self._publish_configuration_result(result)

    def _emit_command_result(
        self,
        request: CommandRequest,
        command,
        *,
        ok: bool,
        message: str | None,
        error_code: str | None,
    ) -> None:
        command_name = command.name if isinstance(command, Enum) else str(command)
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
