
import asyncio
import logging
import multiprocessing as mp
import threading
import time
from contextlib import AsyncExitStack
from copy import deepcopy
from queue import Empty, Full
from uuid import uuid4

from omnibci.application.events import AcquisitionSnapshot, CommandFailureKind, CommandRequest, CommandResult, StreamBarrier
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.acquisition.logging import AcquisitionLogListener
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.async_lifecycle import await_owned
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, StartupChannel, wait_for_startup_report,
)

logger = logging.getLogger(__name__)


class NeuroEngine:
    """
    Owns the event bus, transports, and independent module worker lifetimes.

    Parameters
    ----------
    event_bus:
        Shared EventBus instance (mp.Queue already set on it).
    data_manager:
        Shared buffer between AcquisitionWorker and SignalProcessingWorker.
    acquisition_worker:
        Optional pre-built AcquisitionWorker, otherwise supplied by worker_factory.
    acquisition_command_queue:
        mp.Queue used to send (AcquisitionCommand, payload) pairs to the process.
    signal_processing_worker:
        Optional pre-built SignalProcessingWorker, otherwise supplied by worker_factory.
    processor_command_queue:
        mp.Queue used to send (SignalProcessorCommand, payload) pairs to the process.
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
        debug: bool = False,
        owned_ipc_queues: tuple[mp.Queue, ...] = (),
        startup_channels: tuple[StartupChannel, ...] = (),
        startup_timeout_s: float = 5.0,
        module_revisions: dict[str, str] | None = None,
        engine_revision: str | None = None,
        generation: str | None = None,
        allow_process_termination: bool = True,
        preview_cache: PreviewCache | None = None,
        acquisition_logs: AcquisitionLogListener | None = None,
        worker_factory=None,
        processing_active=None,
        acquisition_stop_event=None,
        data_queue_size: int = 100,
        record_execution=None,
    ) -> None:
        self._record_execution = record_execution
        self.event_bus = event_bus
        self.data_manager = data_manager
        self.preview_cache = preview_cache
        self.acquisition_logs = acquisition_logs
        self.acquisition_worker = acquisition_worker
        self.acquisition_command_queue = acquisition_command_queue
        self.signal_processing_worker = signal_processing_worker
        self.processor_command_queue = processor_command_queue
        self._worker_factory = worker_factory
        self._processing_active = processing_active
        self._acquisition_stop_event = acquisition_stop_event
        self._data_queue_size = max(1, int(data_queue_size))
        self._processing_queue_dirty = False
        self._ready_modules = set()
        self._module_locks = {module: asyncio.Lock() for module in ("acquisition", "processing")}
        # Queues constructed by the composition builder but not otherwise
        # exposed as command queues (EventBus input + DataManager buffer).
        # Injecting them makes ownership explicit without teaching EventBus or
        # DataManager to close resources they merely consume.
        self._owned_ipc_queues = tuple(owned_ipc_queues)
        self._startup_channels = tuple(startup_channels)
        self._startup_channels_closed = not self._startup_channels
        self._startup_timeout_s = startup_timeout_s
        self._module_revisions = dict(module_revisions) if module_revisions is not None else None
        self._engine_revision = engine_revision
        self._generation = generation
        self._allow_process_termination = allow_process_termination
        # Engine-side consumer of stimulation → engine feedback (StimulationAck).
        self.stim_feedback = StimulationFeedbackHandler()
        self._debug = debug
        # The public stop flag wakes the engine loop across thread boundaries.
        self._should_stop = False
        self._stop_event: "asyncio.Event | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # ``run()`` executes in NeuroEngineService's dedicated thread.  This
        # thread-safe barrier is the boundary between "the thread was
        # scheduled" and "foundation listeners are ready".
        self._startup_complete = threading.Event()
        self._startup_error: BaseException | None = None

        # Explicit ownership flags make startup transactional and shutdown
        # idempotent.  A failed bind must unwind workers/bus/queues instead of
        # leaving a half-started engine graph behind.
        self._acquisition_started = False
        self._processor_started = False
        self._event_bus_started = False
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

    def subscribe(self, event_type, handler) -> None:
        self.event_bus.register_event_listener(event_type, handler)

    def dispatch(self, event: Event) -> None:
        self.event_bus.put_event(event)

    def request_stop(self) -> None:
        self.should_stop = True

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
        the EventBus consumer and every listening transport are ready.
        Business modules have separate startup acknowledgements.
        """
        if not self._startup_complete.wait(timeout):
            raise TimeoutError("NeuroEngine startup timed out")
        if self._startup_error is not None:
            raise RuntimeError("NeuroEngine startup failed") from self._startup_error

    @property
    def shutdown_complete(self) -> bool:
        """Whether this single-use graph released every resource it owns."""
        return self._shutdown_complete

    @property
    def processing_queue_dirty(self) -> bool:
        return self._processing_queue_dirty

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
                self._event_bus_started)) or (
                    self.preview_cache is not None and not self.preview_cache.stopped) or (
                    self.acquisition_logs is not None and not self.acquisition_logs.stopped):
            raise RuntimeError("NeuroEngine is in a partial lifecycle state")
        if self._shutdown_complete:
            raise RuntimeError("A stopped NeuroEngine graph cannot be restarted")

        try:
            self._loop = asyncio.get_running_loop()
            # Install every internal consumer before workers can publish.
            self._register_event_listeners()
            if self.acquisition_logs is not None:
                self.acquisition_logs.start()
            if self.preview_cache is not None:
                await self.preview_cache.start()
            # Mark EventBus ownership before awaiting start() so partial
            # startup failures still run stop() during rollback.
            self._event_bus_started = True
            await self.event_bus.start()
            self._started = True
        except BaseException:
            logger.exception("NeuroEngine startup failed; rolling back")
            try:
                await self._shutdown_resources()
            except Exception:
                logger.exception("NeuroEngine startup rollback was incomplete")
            raise
        logger.info("NeuroEngine started")

    def is_module_running(self, module: str) -> bool:
        if module not in {"acquisition", "processing"}:
            raise ValueError("Unknown worker module")
        worker = self.acquisition_worker if module == "acquisition" else self.signal_processing_worker
        try:
            return module in self._ready_modules and worker is not None and worker.is_alive()
        except ValueError:
            return False

    async def start_module(self, module: str, snapshot: dict) -> None:
        await self._on_engine_loop(self._change_module(module, deepcopy(snapshot)))

    async def stop_module(self, module: str) -> None:
        await self._on_engine_loop(self._change_module(module, None))

    async def _change_module(self, module, snapshot):
        if module not in self._module_locks:
            raise ValueError("Unknown worker module")
        async with self._module_locks[module]:
            if snapshot is None:
                await self._stop_module(module)
            else:
                await self._start_module(module, snapshot)

    async def _on_engine_loop(self, operation):
        if self._loop is None or not self._loop.is_running():
            operation.close()
            raise RuntimeError("NeuroEngine loop is unavailable")
        if asyncio.get_running_loop() is self._loop:
            return await operation
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        return await asyncio.wrap_future(future)

    async def _start_module(self, module, snapshot):
        if module not in {"acquisition", "processing"}:
            raise ValueError("Unknown worker module")
        if not self._started:
            raise RuntimeError("Start NeuroEngine before starting a module")
        if (snapshot.get("CONFIG.generation") != self._generation
                or snapshot.get("CONFIG.engine_revision") != self._engine_revision):
            raise ValueError("Module belongs to a different Engine generation or configuration")
        if self.is_module_running(module):
            return
        if module == "processing" and self._processing_queue_dirty:
            raise RuntimeError(
                "Processing input queue cleanup was not confirmed; stop and restart the Engine"
            )
        flag = "_acquisition_started" if module == "acquisition" else "_processor_started"
        worker_attr = "acquisition_worker" if module == "acquisition" else "signal_processing_worker"
        if getattr(self, flag):
            await self._stop_module(module)
        if module == "acquisition" and self._acquisition_stop_event is not None:
            self._acquisition_stop_event.clear()
        queue = self.acquisition_command_queue if module == "acquisition" else self.processor_command_queue
        if self._worker_factory is not None:
            self._discard_queued(queue)
            channel = self._worker_factory(module, snapshot)
            setattr(self, worker_attr, channel.process)
            self._startup_channels += (channel,)
            self._startup_channels_closed = False
        worker = getattr(self, worker_attr)
        if worker is None:
            raise RuntimeError(f"{module} worker is unavailable")
        channels = tuple(channel for channel in self._startup_channels if channel.process is worker)
        revisions = snapshot.get("CONFIG.module_revisions")
        if revisions is not None:
            self._module_revisions = {**(self._module_revisions or {}), module: revisions.get(module)}
        try:
            worker.start()
            setattr(self, flag, True)
            self._close_parent_startup_sender(worker)
            await self._wait_for_workers(channels)
            if not self._started:
                raise RuntimeError("NeuroEngine is stopping")
            if not worker.is_alive():
                raise ChildProcessStartupError(f"{module} subprocess exited before becoming ready")
            self._ready_modules.add(module)
        except BaseException:
            self._close_startup_channels(channels)
            await self._stop_module(module)
            raise

    async def _stop_module(self, module):
        if module not in {"acquisition", "processing"}:
            raise ValueError("Unknown worker module")
        self._ready_modules.discard(module)
        if module == "processing" and self._processing_active is not None:
            self._processing_active.clear()
        flag = "_acquisition_started" if module == "acquisition" else "_processor_started"
        if not getattr(self, flag):
            return
        worker = self.acquisition_worker if module == "acquisition" else self.signal_processing_worker
        queue = self.acquisition_command_queue if module == "acquisition" else self.processor_command_queue
        command = AcquisitionCommand.stop_acquisition if module == "acquisition" else SignalProcessorCommand.should_stop
        if module == "acquisition" and self._acquisition_stop_event is not None:
            self._acquisition_stop_event.set()
        self._put_stop_command(queue, command, module)
        if module == "acquisition" and not self._processor_started:
            await asyncio.to_thread(self._join_acquisition_with_drain, worker, "acquisition")
        else:
            await asyncio.to_thread(self._join_process, worker, module,
                                    allow_process_termination=self._allow_process_termination)
        setattr(self, flag, False)
        if self._worker_factory is not None:
            self._discard_queued(queue)
        if module == "acquisition":
            self.event_bus.put_event(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))

    async def drain_abandoned_processing_queue(self, timeout_s: float = 3.0) -> bool:
        """Confirm an empty FIFO boundary on the shared data queue after consumer exit."""
        return await self._on_engine_loop(self._drain_abandoned_processing_queue(timeout_s))

    async def _drain_abandoned_processing_queue(self, timeout_s: float) -> bool:
        async with self._module_locks["processing"]:
            if self._processor_started:
                self._processing_queue_dirty = True
                return False
            if self._processing_active is not None:
                self._processing_active.clear()
            if not isinstance(self.data_manager, DataManager):
                self._processing_queue_dirty = False
                return True
            if self.is_module_running("acquisition"):
                token = uuid4().hex
                try:
                    self.acquisition_command_queue.put_nowait((AcquisitionCommand.flush_processing_queue, token))
                except Exception:
                    self._processing_queue_dirty = True
                    return False
                confirmed = await asyncio.to_thread(self._drain_until_flush_barrier, token, timeout_s)
                self._processing_queue_dirty = not confirmed
                return confirmed
            if not self._acquisition_started:
                await asyncio.to_thread(self._drain_until_empty, min(timeout_s, 0.25))
                self._processing_queue_dirty = False
                return True
            self._processing_queue_dirty = True
            return False

    def _drain_until_flush_barrier(self, token: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        max_items = max(self._data_queue_size * 4, 256)
        drained = 0
        while time.monotonic() < deadline and drained < max_items:
            remaining = max(0.01, min(0.05, deadline - time.monotonic()))
            try:
                item = self.data_manager.get_data(timeout=remaining)
            except Exception:
                return False
            if item is None:
                continue
            if (isinstance(item, StreamBarrier)
                    and item.command == "FlushProcessingQueue"
                    and item.command_id == token):
                return True
            drained += 1
        return False

    def _drain_until_empty(self, timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        max_items = max(self._data_queue_size * 4, 256)
        drained = 0
        while time.monotonic() < deadline and drained < max_items:
            try:
                item = self.data_manager.get_data(timeout=0.02)
            except Exception:
                break
            if item is None:
                break
            drained += 1
        return drained

    def _join_acquisition_with_drain(self, process: mp.Process, name: str, timeout: float = 3.0) -> None:
        """Drain a retiring EEG queue while AcquisitionWorker exits so Queue._finalize_join cannot block."""
        if not isinstance(self.data_manager, DataManager) or self._processor_started:
            self._join_process(
                process, name, timeout=timeout,
                allow_process_termination=self._allow_process_termination,
            )
            return
        deadline = time.monotonic() + timeout
        process.join(timeout=min(0.1, timeout))
        if process.is_alive():
            max_items = max(self._data_queue_size * 4, 256)
            drained = 0
            while process.is_alive() and time.monotonic() < deadline and drained < max_items:
                try:
                    item = self.data_manager.get_data(timeout=0.05)
                except Exception:
                    break
                if item is None:
                    process.join(timeout=0.02)
                    continue
                drained += 1
        remaining = max(0.0, deadline - time.monotonic())
        self._join_process(
            process, name, timeout=remaining,
            allow_process_termination=self._allow_process_termination,
        )

    @staticmethod
    def _discard_queued(queue):
        while True:
            try:
                queue.get(timeout=0.01)
            except Empty:
                return

    def _close_parent_startup_sender(self, process) -> None:
        # Process.start() has copied the sender into the child. Retaining the
        # parent's copy would keep a dead child's pipe artificially open.
        for channel in self._startup_channels:
            if channel.process is process:
                channel.reporter.close()

    async def _wait_for_workers(self, channels) -> None:
        # Explicit startup channels distinguish managed workers from injected test
        # processes. Never infer readiness support from mock attributes.
        try:
            results = await asyncio.gather(*(
                asyncio.to_thread(
                    wait_for_startup_report, channel.receiver, channel.process,
                    component=channel.component, timeout_s=self._startup_timeout_s,
                    expected_module_revisions=({
                        {"Acquisition": "acquisition", "Signal Processing": "processing"}[channel.component]:
                        self._module_revisions[{"Acquisition": "acquisition", "Signal Processing": "processing"}[channel.component]],
                    } if self._module_revisions is not None else None),
                    expected_engine_revision=self._engine_revision,
                    expected_generation=self._generation,
                    expected_execution=channel.expected_execution,
                ) for channel in channels
            ), return_exceptions=True)
            for result in results:
                if (not isinstance(result, BaseException) and result.execution is not None
                        and self._record_execution is not None):
                    await await_owned(asyncio.create_task(asyncio.to_thread(self._record_execution, result.execution)))
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            for channel in channels:
                if not channel.process.is_alive():
                    raise ChildProcessStartupError(
                        f"{channel.component} subprocess exited before becoming ready"
                    )
        finally:
            self._close_startup_channels(channels)

    def _close_startup_channels(self, channels=None) -> None:
        if self._startup_channels_closed:
            return
        errors = []
        channels = self._startup_channels if channels is None else channels
        channels = tuple(channel for channel in channels if channel in self._startup_channels)
        for channel in channels:
            for handle in (channel.reporter, channel.receiver):
                try:
                    handle.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Worker startup channel cleanup failed", errors)
        self._startup_channels = tuple(channel for channel in self._startup_channels if channel not in channels)
        self._startup_channels_closed = not self._startup_channels

    async def _main_loop(self) -> None:
        # The timeout also bounds detection of a worker that exits after READY.
        while not self.should_stop:
            for started, process, module in (
                (self._acquisition_started, self.acquisition_worker, "acquisition"),
                (self._processor_started, self.signal_processing_worker, "processing"),
            ):
                lock = self._module_locks[module]
                if lock.locked():
                    continue
                async with lock:
                    if started and not process.is_alive():
                        logger.error("%s subprocess exited unexpectedly (exit code %s)", module, process.exitcode)
                        if module == "acquisition":
                            if self._processing_active is not None:
                                self._processing_active.clear()
                            if self.is_module_running("processing"):
                                self._make_command_router(
                                    self.processor_command_queue, SignalProcessorCommand.reset_input,
                                    event_bus=self.event_bus,
                                )(Event(EventType.ResetProcessingInput, CommandRequest(uuid4().hex, {
                                    "generation": self._generation, "engine_revision": self._engine_revision,
                                    "expected_acquisition_revision": (self._module_revisions or {}).get("acquisition"),
                                })))
                        try:
                            await self._stop_module(module)
                        except Exception:
                            logger.exception("Could not release failed %s worker", module)
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
        self._started = False
        async with AsyncExitStack() as locks:
            for lock in self._module_locks.values():
                await locks.enter_async_context(lock)
            await self._release_resources()

    async def _release_resources(self) -> None:
        """Release every resource owned by this engine graph, best-effort.

        Workers are stopped while the EventBus is still consuming so their
        final state events are not stranded.  Blocking ``Process.join`` calls
        run off the asyncio loop, allowing those events and socket close tasks
        to continue making progress.
        """
        errors: list[Exception] = []
        self._started = False
        self._ready_modules.clear()
        if self._processing_active is not None:
            self._processing_active.clear()
        try:
            self._close_startup_channels()
        except Exception as exc:
            errors.append(exc)

        if self._acquisition_started:
            if self._acquisition_stop_event is not None:
                self._acquisition_stop_event.set()
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

        # Join the consumer before draining and joining the producer so a full
        # EEG pipe cannot deadlock AcquisitionWorker's Queue._finalize_join.
        if self._processor_started:
            try:
                await asyncio.to_thread(
                    self._join_process, self.signal_processing_worker, "SignalProcessingWorker",
                    allow_process_termination=self._allow_process_termination,
                )
            except Exception as exc:  # keep releasing the remaining owners
                errors.append(exc)
                logger.exception("Error stopping %s", "SignalProcessingWorker")
            else:
                self._processor_started = False

        if self._acquisition_started:
            try:
                if not self._processor_started:
                    await asyncio.to_thread(
                        self._join_acquisition_with_drain, self.acquisition_worker, "AcquisitionWorker",
                    )
                else:
                    await asyncio.to_thread(
                        self._join_process, self.acquisition_worker, "AcquisitionWorker",
                        allow_process_termination=self._allow_process_termination,
                    )
            except Exception as exc:  # keep releasing the remaining owners
                errors.append(exc)
                logger.exception("Error stopping %s", "AcquisitionWorker")
            else:
                self._acquisition_started = False

        if self.acquisition_logs is not None and not self._acquisition_started:
            try:
                if not await asyncio.to_thread(self.acquisition_logs.stop):
                    raise RuntimeError("Acquisition log consumer did not stop")
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error stopping acquisition log consumer")

        if self.preview_cache is not None:
            try:
                await self.preview_cache.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error stopping acquisition preview consumer")

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
        logs_stopped = self.acquisition_logs is None or self.acquisition_logs.stopped
        if workers_stopped and preview_stopped and logs_stopped and not self._event_bus_started and not self._queues_closed:
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
            and not self._event_bus_started
            and self._queues_closed
            and self._startup_channels_closed
            and (self.preview_cache is None or self.preview_cache.stopped)
            and logs_stopped
        )
        if errors:
            raise ExceptionGroup("NeuroEngine shutdown failed", errors)

    def reconcile_shutdown(self) -> bool:
        """Retry synchronous cleanup off the retired Engine loop after an earlier timeout."""
        if self._shutdown_complete:
            return True
        if self._processing_active is not None:
            self._processing_active.clear()
        errors: list[Exception] = []
        try:
            self._close_startup_channels()
        except Exception as exc:
            errors.append(exc)
        if self._processor_started:
            self._put_stop_command(
                self.processor_command_queue,
                SignalProcessorCommand.should_stop,
                "SignalProcessingWorker",
            )
            try:
                self._join_process(
                    self.signal_processing_worker, "SignalProcessingWorker",
                    allow_process_termination=self._allow_process_termination,
                )
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error reconciling SignalProcessingWorker shutdown")
            else:
                self._processor_started = False
        if self._acquisition_started:
            if self._acquisition_stop_event is not None:
                self._acquisition_stop_event.set()
            self._put_stop_command(
                self.acquisition_command_queue,
                AcquisitionCommand.stop_acquisition,
                "AcquisitionWorker",
            )
            try:
                if not self._processor_started:
                    self._join_acquisition_with_drain(self.acquisition_worker, "AcquisitionWorker")
                else:
                    self._join_process(
                        self.acquisition_worker, "AcquisitionWorker",
                        allow_process_termination=self._allow_process_termination,
                    )
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error reconciling AcquisitionWorker shutdown")
            else:
                self._acquisition_started = False
        if self.acquisition_logs is not None and not self._acquisition_started and not self.acquisition_logs.stopped:
            try:
                if not self.acquisition_logs.stop():
                    raise RuntimeError("Acquisition log consumer did not stop")
            except Exception as exc:
                errors.append(exc)
                logger.exception("Error reconciling acquisition log consumer")
        workers_stopped = not (self._acquisition_started or self._processor_started)
        preview_stopped = self.preview_cache is None or self.preview_cache.stopped
        logs_stopped = self.acquisition_logs is None or self.acquisition_logs.stopped
        if workers_stopped and preview_stopped and logs_stopped and not self._event_bus_started and not self._queues_closed:
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
                    logger.exception("Error closing an engine-owned IPC queue during reconciliation")
            if queue_errors:
                errors.extend(queue_errors)
            else:
                self._queues_closed = True
        self._shutdown_complete = (
            workers_stopped
            and not self._event_bus_started
            and self._queues_closed
            and self._startup_channels_closed
            and preview_stopped
            and logs_stopped
        )
        if errors:
            raise ExceptionGroup("NeuroEngine shutdown reconciliation failed", errors)
        return self._shutdown_complete

    @staticmethod
    def _put_stop_command(queue: mp.Queue, command, owner: str) -> None:
        """Never block the engine loop on a full bounded command queue."""
        try:
            queue.put_nowait((command, None))
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
        """Map each event to the worker queue and command receiving its payload."""
        acq = self.acquisition_command_queue
        proc = self.processor_command_queue
        return {
            # Acquisition control requests carry CommandRequest so the worker can
            # return a correlated terminal CommandResult to the REST caller.
            EventType.ConnectDataProvider:            (acq, AcquisitionCommand.connect),
            EventType.DisconnectDataProvider:         (acq, AcquisitionCommand.disconnect),
            EventType.StartStream:                    (acq, AcquisitionCommand.start_stream),
            EventType.StartAcquisitionPreview:        (acq, AcquisitionCommand.start_preview),
            EventType.GetAcquisitionPreview:          (acq, AcquisitionCommand.get_preview),
            EventType.GetSourceMetadata:              (acq, AcquisitionCommand.get_metadata),
            EventType.StopAndDrain:                   (acq, AcquisitionCommand.stop_and_drain),
            EventType.AttachProcessingInput:          (acq, AcquisitionCommand.attach_processing),
            EventType.DetachProcessingInput:          (acq, AcquisitionCommand.detach_processing),
            EventType.GetSourceCheckpoint:            (acq, AcquisitionCommand.get_checkpoint),
            EventType.RestoreSourceCheckpoint:        (acq, AcquisitionCommand.restore_checkpoint),
            EventType.StageProcessingConfiguration:   (proc, SignalProcessorCommand.stage_configuration),
            EventType.CancelProcessingConfiguration:  (proc, SignalProcessorCommand.cancel_configuration),
            EventType.PrepareSignalProcessing:        (proc, SignalProcessorCommand.prepare_signal),
            EventType.ResetProcessingInput:           (proc, SignalProcessorCommand.reset_input),
        }

    def read_acquisition_preview(self, capture_id: str | None, sequence_id: int | None = None) -> dict:
        if self.preview_cache is None:
            raise RuntimeError("Acquisition preview is unavailable")
        return self.preview_cache.read(capture_id, sequence_id)

    def _on_processing_input_fault(self, event):
        fault = event.data
        if (not isinstance(fault, dict)
                or fault.get("generation") != self._generation
                or fault.get("engine_revision") != self._engine_revision
                or fault.get("acquisition_revision") != (self._module_revisions or {}).get("acquisition")
                or not isinstance(fault.get("input_id"), str)
                or not fault["input_id"]
                or not self.is_module_running("processing")):
            return
        self._make_command_router(
            self.processor_command_queue, SignalProcessorCommand.reset_input, event_bus=self.event_bus,
        )(Event(EventType.ResetProcessingInput, CommandRequest(uuid4().hex, {
            "generation": self._generation, "engine_revision": self._engine_revision,
            "expected_acquisition_revision": fault["acquisition_revision"], "input_id": fault["input_id"],
        })))

    def _register_event_listeners(self) -> None:
        for event_type, (queue, command) in self._command_routes().items():
            module = "acquisition" if queue is self.acquisition_command_queue else "processing"
            self.event_bus.register_event_listener(
                event_type, self._make_command_router(
                    queue, command, event_bus=self.event_bus,
                    is_running=lambda module=module: self.is_module_running(module),
                )
            )
        self.event_bus.register_event_listener(EventType.ProcessingInputFault, self._on_processing_input_fault)
        # Stimulation → engine inbound feedback (routed in by the stimulation
        # PeerChannel). The engine now consumes acks, not just the UI.
        self.event_bus.register_event_listener(
            EventType.StimulationAck, self.stim_feedback.on_ack
        )

    @staticmethod
    def _make_command_router(queue: mp.Queue, command,
                             *, event_bus: EventBus, is_running=None) -> "callable":
        """Enqueue ``(command, event.data)`` and report rejected requests."""
        def _route(event) -> None:
            if is_running is not None and not is_running():
                request = CommandRequest.from_value(event.data)
                if request is not None:
                    event_bus.put_event(Event(EventType.CommandResult, CommandResult(
                        command_id=request.command_id, command=event.type.name, ok=False,
                        message="Start the module before sending commands",
                        error_code="MODULE_NOT_RUNNING", failure_kind=CommandFailureKind.REJECTED,
                    )))
                return
            try:
                queue.put_nowait((command, event.data))
            except Full:
                request = CommandRequest.from_value(event.data)
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
