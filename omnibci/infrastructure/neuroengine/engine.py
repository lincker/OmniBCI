
import asyncio
import logging
import multiprocessing as mp
import threading
from queue import Full

from omnibci.application.events import CommandFailureKind, CommandRequest, CommandResult
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway
from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, StartupChannel, wait_for_startup_report,
)

logger = logging.getLogger(__name__)


class NeuroEngine:
    """
    Pure orchestrator — all dependencies injected, none constructed here.

    Parameters
    ----------
    event_bus:
        Shared EventBus instance (mp.Queue already set on it).
    data_manager:
        Shared buffer between AcquisitionWorker and SignalProcessingWorker.
    acquisition_worker:
        Pre-built AcquisitionWorker (not yet started).
    acquisition_command_queue:
        mp.Queue used to send AcquisitionCommand values to the process.
    signal_processing_worker:
        Pre-built SignalProcessingWorker (not yet started).
    processor_command_queue:
        mp.Queue used to send SignalProcessorCommand values to the process.
    peer_gateway:
        Routes EventBus events to/from external peers (stimulation, actuation).
    owned_ipc_queues:
        Additional builder-created queues whose close lifecycle belongs to this
        single-use engine graph (EventBus input and DataManager buffer).
    allow_process_termination:
        False for desktop hosts: retain unresponsive workers for an explicit
        user decision instead of automatically terminating them.
    """

    def __init__(
        self,
        event_bus: EventBus,
        data_manager: DataManager,
        acquisition_worker: AcquisitionWorker,
        acquisition_command_queue: mp.Queue,
        signal_processing_worker: SignalProcessingWorker,
        processor_command_queue: mp.Queue,
        peer_gateway: PeerGateway,
        actuation_translator: "ActuationCommandTranslator | None" = None,
        actuation_feedback: "ActuationFeedbackHandler | None" = None,
        debug: bool = False,
        owned_ipc_queues: tuple[mp.Queue, ...] = (),
        startup_channels: tuple[StartupChannel, ...] = (),
        startup_timeout_s: float = 5.0,
        config_revision: str | None = None,
        generation: str | None = None,
        allow_process_termination: bool = True,
        preview_cache: PreviewCache | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.data_manager = data_manager
        self.preview_cache = preview_cache
        self.acquisition_worker = acquisition_worker
        self.acquisition_command_queue = acquisition_command_queue
        self.signal_processing_worker = signal_processing_worker
        self.processor_command_queue = processor_command_queue
        self.peer_gateway = peer_gateway
        # Queues constructed by the composition builder but not otherwise
        # exposed as command queues (EventBus input + DataManager buffer).
        # Injecting them makes ownership explicit without teaching EventBus or
        # DataManager to close resources they merely consume.
        self._owned_ipc_queues = tuple(owned_ipc_queues)
        self._startup_channels = tuple(startup_channels)
        self._startup_channels_closed = not self._startup_channels
        self._startup_timeout_s = startup_timeout_s
        self._config_revision = config_revision
        self._generation = generation
        self._allow_process_termination = allow_process_termination
        # Engine-side consumer of stimulation → engine feedback (StimulationAck).
        self.stim_feedback = StimulationFeedbackHandler()
        # Actuation ACL + feedback — present only when the actuation peer is
        # enabled (builder injects them; None disables all actuation wiring).
        self.actuation_translator = actuation_translator
        self.actuation_feedback = actuation_feedback
        self._debug = debug
        # The public stop flag wakes the engine loop across thread boundaries.
        self._should_stop = False
        self._stop_event: "asyncio.Event | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # ``run()`` executes in NeuroEngineService's dedicated thread.  This
        # thread-safe barrier is the boundary between "the thread was
        # scheduled" and "listeners and configured workers are ready".
        self._startup_complete = threading.Event()
        self._startup_error: BaseException | None = None

        # Explicit ownership flags make startup transactional and shutdown
        # idempotent.  A failed bind must unwind workers/bus/queues instead of
        # leaving a half-started engine graph behind.
        self._acquisition_started = False
        self._processor_started = False
        self._event_bus_started = False
        self._gateway_started = False
        self._started = False
        self._queues_closed = False
        self._shutdown_complete = False

    # ------------------------------------------------------------------
    # Stop flag
    # ------------------------------------------------------------------

    @property
    def should_stop(self) -> bool:
        return self._should_stop

    @should_stop.setter
    def should_stop(self, value: bool) -> None:
        """Set the stop flag and wake the engine loop from any thread."""
        self._should_stop = bool(value)
        if value and self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking entry point — call from a dedicated thread."""
        try:
            asyncio.run(self.serve(), debug=self._debug)
        except BaseException as exc:
            # Publish startup failures (most importantly listen/bind errors) to
            # the service thread before re-raising for normal error logging.
            if not self._startup_complete.is_set():
                self._startup_error = exc
                self._startup_complete.set()
            raise

    def wait_until_started(self, timeout: float | None = None) -> None:
        """Block the caller until async startup either succeeds or fails.

        ``Thread.start()`` only says that the OS thread exists.  This method is
        the readiness contract used by the application service: success means
        the EventBus consumer, every listening transport, and all managed
        workers have completed initialization for this configuration.
        """
        if not self._startup_complete.wait(timeout):
            raise TimeoutError("NeuroEngine startup timed out")
        if self._startup_error is not None:
            raise RuntimeError("NeuroEngine startup failed") from self._startup_error

    @property
    def shutdown_complete(self) -> bool:
        """Whether this single-use graph released every resource it owns."""
        return self._shutdown_complete

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            await self._start()
            self._startup_complete.set()
            await self._main_loop()
        finally:
            # _start() is transactional, so this is safe after both normal
            # execution and partial startup failure.
            await self._stop()

    # ------------------------------------------------------------------
    # Internal lifecycle steps
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        if self._started:
            return
        if any((self._acquisition_started, self._processor_started,
                self._event_bus_started, self._gateway_started)) or (
                    self.preview_cache is not None and not self.preview_cache.stopped):
            raise RuntimeError("NeuroEngine is in a partial lifecycle state")
        if self._shutdown_complete:
            raise RuntimeError("A stopped NeuroEngine graph cannot be restarted")

        try:
            # Install every internal consumer before workers or peers can
            # publish.  Registering after the gateway bind left a small window
            # where a valid inbound event could be consumed and then lost.
            self._register_event_listeners()
            if self.preview_cache is not None:
                await self.preview_cache.start()
            self.acquisition_worker.start()
            self._acquisition_started = True
            self._close_parent_startup_sender(self.acquisition_worker)
            self.signal_processing_worker.start()
            self._processor_started = True
            self._close_parent_startup_sender(self.signal_processing_worker)
            await self.event_bus.start()
            self._event_bus_started = True
            # start() may allocate a listener and then fail while rolling it
            # back.  Record the cleanup obligation before awaiting so a failed
            # rollback keeps the engine graph dirty and blocks replacement.
            self._gateway_started = True
            await self.peer_gateway.start()
            await self._wait_for_workers()
            self._started = True
        except BaseException:
            logger.exception("NeuroEngine startup failed; rolling back")
            try:
                await self._shutdown_resources()
            except Exception:
                logger.exception("NeuroEngine startup rollback was incomplete")
            raise
        logger.info("NeuroEngine started")

    def _close_parent_startup_sender(self, process) -> None:
        # Process.start() has copied the sender into the child. Retaining the
        # parent's copy would keep a dead child's pipe artificially open.
        for channel in self._startup_channels:
            if channel.process is process:
                channel.reporter.close()

    async def _wait_for_workers(self) -> None:
        # Explicitly injected channels distinguish managed workers from legacy
        # manual engine graphs. Never infer readiness support from mock attributes.
        try:
            results = await asyncio.gather(*(
                asyncio.to_thread(
                    wait_for_startup_report, channel.receiver, channel.process,
                    component=channel.component, timeout_s=self._startup_timeout_s,
                    expected_revision=self._config_revision,
                    expected_generation=self._generation,
                ) for channel in self._startup_channels
            ), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            for channel in self._startup_channels:
                if not channel.process.is_alive():
                    raise ChildProcessStartupError(
                        f"{channel.component} subprocess exited before all workers became ready"
                    )
        finally:
            self._close_startup_channels()

    def _close_startup_channels(self) -> None:
        if self._startup_channels_closed:
            return
        errors = []
        for channel in self._startup_channels:
            for handle in (channel.reporter, channel.receiver):
                try:
                    handle.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Worker startup channel cleanup failed", errors)
        self._startup_channels_closed = True

    async def _main_loop(self) -> None:
        # The timeout also bounds detection of a worker that exits after READY.
        while not self.should_stop:
            for started, process, name in (
                (self._acquisition_started, self.acquisition_worker, "Acquisition"),
                (self._processor_started, self.signal_processing_worker, "Signal Processing"),
            ):
                if started and not process.is_alive():
                    raise RuntimeError(
                        f"{name} subprocess exited unexpectedly (exit code {process.exitcode})"
                    )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def _stop(self) -> None:
        if self._shutdown_complete:
            return
        logger.info("NeuroEngine: initiating shutdown")
        await self._shutdown_resources()
        logger.info("NeuroEngine: shutdown complete")

    async def _shutdown_resources(self) -> None:
        """Release every resource owned by this engine graph, best-effort.

        Workers are stopped while the EventBus is still consuming so their
        final state events are not stranded.  Blocking ``Process.join`` calls
        run off the asyncio loop, allowing those events and socket close tasks
        to continue making progress.
        """
        errors: list[Exception] = []
        self._started = False
        try:
            self._close_startup_channels()
        except Exception as exc:
            errors.append(exc)

        if self._acquisition_started:
            self._put_stop_command(
                self.acquisition_command_queue,
                AcquisitionCommand.stop_acquisition,
                "AcquisitionWorker",
            )
        if self._processor_started:
            self._put_stop_command(
                self.processor_command_queue,
                SignalProcessorCommand.should_stop,
                "SignalProcessingWorker",
            )

        for attr, process, name in (
            ("_acquisition_started", self.acquisition_worker, "AcquisitionWorker"),
            ("_processor_started", self.signal_processing_worker, "SignalProcessingWorker"),
        ):
            if not getattr(self, attr):
                continue
            try:
                await asyncio.to_thread(
                    self._join_process, process, name,
                    allow_process_termination=self._allow_process_termination,
                )
            except Exception as exc:  # keep releasing the remaining owners
                errors.append(exc)
                logger.exception("Error stopping %s", name)
            else:
                setattr(self, attr, False)

        if self.preview_cache is not None:
            try:
                await self.preview_cache.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error stopping acquisition preview consumer")

        if self._gateway_started:
            try:
                await self.peer_gateway.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error stopping PeerGateway")
            else:
                self._gateway_started = False

        if self._event_bus_started:
            try:
                await self.event_bus.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error stopping EventBus")
            else:
                self._event_bus_started = False

        # A still-live worker owns its queue endpoints.  Retain those handles on
        # escalation failure instead of advertising a clean shutdown and
        # allowing a second graph to be built beside it.
        workers_stopped = not (
            self._acquisition_started or self._processor_started
        )
        preview_stopped = self.preview_cache is None or self.preview_cache.stopped
        if workers_stopped and preview_stopped and not self._event_bus_started and not self._queues_closed:
            queue_errors = []
            for queue in (
                self.processor_command_queue,
                self.acquisition_command_queue,
                *self._owned_ipc_queues,
            ):
                try:
                    self._close_queue(queue)
                except Exception as exc:
                    queue_errors.append(exc)
                    logger.exception("Error closing an engine-owned IPC queue")
            if queue_errors:
                errors.extend(queue_errors)
            else:
                self._queues_closed = True

        self._shutdown_complete = (
            workers_stopped
            and not self._gateway_started
            and not self._event_bus_started
            and self._queues_closed
            and self._startup_channels_closed
            and (self.preview_cache is None or self.preview_cache.stopped)
        )
        if errors:
            raise ExceptionGroup("NeuroEngine shutdown failed", errors)

    @staticmethod
    def _put_stop_command(queue: mp.Queue, command, owner: str) -> None:
        """Never block the engine loop on a full bounded command queue."""
        try:
            put_nowait = getattr(queue, "put_nowait", None)
            if callable(put_nowait):
                put_nowait(command)
            else:  # simple test doubles / compatible queue adapters
                queue.put(command)
        except Full:
            logger.warning("%s command queue is full; escalating during join", owner)
        except Exception as exc:
            logger.warning("Could not enqueue stop command for %s: %s", owner, exc)

    # ------------------------------------------------------------------
    # Event routing — translate EventBus events into process commands
    #
    # The routing table is the single source of truth for "incoming EventType →
    # (target command queue, command to enqueue)".  Adding a route is one line;
    # there are no per-event handler methods to keep in sync.  Events needing
    # custom behaviour (not a plain queue.put) are registered explicitly below.
    # ------------------------------------------------------------------

    def _command_routes(self) -> dict:
        """
        EventType → (command queue, command, carry_data).

        carry_data=False: enqueue the bare command (legacy behaviour).
        carry_data=True: enqueue ``(command, event.data)`` so the request
        reaches the subprocess.
        """
        acq = self.acquisition_command_queue
        proc = self.processor_command_queue
        return {
            # Acquisition control requests carry CommandRequest so the worker can
            # return a correlated terminal CommandResult to the REST caller.
            EventType.ConnectDataProvider:            (acq, AcquisitionCommand.connect, True),
            EventType.DisconnectDataProvider:         (acq, AcquisitionCommand.disconnect, True),
            EventType.StartStream:                    (acq, AcquisitionCommand.start_stream, True),
            EventType.StartAcquisitionPreview:        (acq, AcquisitionCommand.start_preview, True),
            EventType.StopAcquisitionPreview:         (acq, AcquisitionCommand.stop_preview, True),
            EventType.GetAcquisitionPreview:          (acq, AcquisitionCommand.get_preview, True),
            EventType.GetSourceMetadata:              (acq, AcquisitionCommand.get_metadata, True),
            EventType.StopStream:                     (acq, AcquisitionCommand.stop_stream, True),
            EventType.StopAndDrain:                   (acq, AcquisitionCommand.stop_and_drain, True),
            EventType.GetSourceCheckpoint:            (acq, AcquisitionCommand.get_checkpoint, True),
            EventType.RestoreSourceCheckpoint:        (acq, AcquisitionCommand.restore_checkpoint, True),
            EventType.UpdateAcquisitionRevision:      (acq, AcquisitionCommand.update_revision, True),
            EventType.StageProcessingConfiguration:   (proc, SignalProcessorCommand.stage_configuration, True),
            EventType.CancelProcessingConfiguration:  (proc, SignalProcessorCommand.cancel_configuration, True),
            EventType.UpdateProcessingContext:        (proc, SignalProcessorCommand.update_context, True),
            EventType.PrepareSignalProcessing:        (proc, SignalProcessorCommand.prepare_signal, True),
        }

    def read_acquisition_preview(self, capture_id: str | None, sequence_id: int | None = None) -> dict:
        if self.preview_cache is None:
            raise RuntimeError("Acquisition preview is unavailable")
        return self.preview_cache.read(capture_id, sequence_id)

    def _register_event_listeners(self) -> None:
        for event_type, (queue, command, carry_data) in self._command_routes().items():
            self.event_bus.register_event_listener(
                event_type, self._make_command_router(
                    queue, command, carry_data, event_bus=self.event_bus,
                )
            )
        # Non-routing event: flips the shutdown flag, no command enqueued.
        self.event_bus.register_event_listener(
            EventType.CloseAllNeuroEngineProcessSystem, self._stop_neuro_engine_system
        )
        # Stimulation → engine inbound feedback (routed in by the stimulation
        # PeerChannel). The engine now consumes acks, not just the UI.
        self.event_bus.register_event_listener(
            EventType.StimulationAck, self.stim_feedback.on_ack
        )
        # Actuation wiring (only when enabled): translate each decoded trial into
        # an ActuationCommand, and consume the device's inbound feedback.
        if self.actuation_translator is not None:
            self.event_bus.register_event_listener(
                EventType.TrialCompleted, self.actuation_translator.on_trial_completed
            )
        if self.actuation_feedback is not None:
            for event_type in (
                EventType.ActuationAck,
                EventType.ActuationState,
                EventType.ActuationFault,
            ):
                self.event_bus.register_event_listener(
                    event_type, self.actuation_feedback.on_feedback
                )

    @staticmethod
    def _make_command_router(queue: mp.Queue, command, carry_data: bool = False,
                             *, event_bus: EventBus) -> "callable":
        """
        Build a listener that enqueues *command* on *queue*.

        With carry_data the event's payload travels along as ``(command, data)``;
        otherwise the bare command is enqueued and the event is ignored.
        """
        def _route(event) -> None:
            try:
                queue.put_nowait((command, event.data) if carry_data else command)
            except Full:
                request = CommandRequest.from_value(event.data) if carry_data else None
                if request is None:
                    logger.warning("Command queue is full; rejecting %s", event.type.name)
                    return
                event_bus.put_event(Event(EventType.CommandResult, CommandResult(
                    command_id=request.command_id,
                    command=event.type.name,
                    ok=False,
                    message="Command queue is full; retry when the worker is available",
                    error_code="COMMAND_QUEUE_FULL",
                    failure_kind=CommandFailureKind.REJECTED,
                )))
        return _route

    def _stop_neuro_engine_system(self, *_) -> None:
        self.should_stop = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _join_process(process: mp.Process, name: str, timeout: int = 3,
                      *, allow_process_termination: bool = True) -> None:
        process.join(timeout=timeout)
        if process.is_alive():
            if not allow_process_termination:
                raise RuntimeError(
                    f"{name} did not stop gracefully; process retained for an explicit force-exit decision"
                )
            logger.warning("%s did not exit gracefully; terminating", name)
            process.terminate()
            process.join(timeout=timeout)
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                logger.warning("%s ignored terminate; killing", name)
                kill()
                process.join(timeout=timeout)
        if process.is_alive():
            raise RuntimeError(f"{name} is still alive after shutdown escalation")
        close = getattr(process, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _close_queue(queue: mp.Queue) -> None:
        """Close a queue without waiting forever for discarded backlog.

        At this point every producer/consumer process and the EventBus have
        stopped, so queued work has no valid recipient.  ``join_thread()`` can
        block forever if a feeder is flushing into a full pipe whose reader
        exited.  Cancelling that implicit join makes the discard policy
        explicit; close failures are propagated so ``shutdown_complete`` can
        never claim success for an owned handle that was not released.
        """
        errors = []
        cancel_join = getattr(queue, "cancel_join_thread", None)
        if callable(cancel_join):
            try:
                cancel_join()
            except Exception as exc:
                errors.append(exc)
        try:
            queue.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise ExceptionGroup("IPC queue cleanup failed", errors)
