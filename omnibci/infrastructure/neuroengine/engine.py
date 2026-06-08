from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp

from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import EventType
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.server.management import ServerMessageManagement

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
    server_message_management:
        Manages the NetworkServer that pushes results to external clients.
    """

    def __init__(
        self,
        event_bus: EventBus,
        data_manager: DataManager,
        acquisition_worker: AcquisitionWorker,
        acquisition_command_queue: mp.Queue,
        signal_processing_worker: SignalProcessingWorker,
        processor_command_queue: mp.Queue,
        server_message_management: ServerMessageManagement,
        debug: bool = False,
    ) -> None:
        self.event_bus = event_bus
        self.data_manager = data_manager
        self.acquisition_worker = acquisition_worker
        self.acquisition_command_queue = acquisition_command_queue
        self.signal_processing_worker = signal_processing_worker
        self.processor_command_queue = processor_command_queue
        self.server_message_management = server_message_management
        self._debug = debug
        self.should_stop = False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking entry point — call from a dedicated thread."""
        asyncio.run(self.serve(), debug=self._debug)

    async def serve(self) -> None:
        await self._start()
        await self._main_loop()
        await self._stop()

    # ------------------------------------------------------------------
    # Internal lifecycle steps
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        self.acquisition_worker.start()
        self.signal_processing_worker.start()
        await self.event_bus.start()
        self.server_message_management.start()
        self._register_event_listeners()
        logger.info("NeuroEngine started")

    async def _main_loop(self) -> None:
        while not self.should_stop:
            await asyncio.sleep(0.05)

    async def _stop(self) -> None:
        logger.info("NeuroEngine: initiating shutdown")

        # Tell both sub-processes to stop, then tear down bus/server/queues.
        self.acquisition_command_queue.put(AcquisitionCommand.stop_acquisition)
        self.processor_command_queue.put(SignalProcessorCommand.should_stop)
        self.event_bus.stop()
        await self.server_message_management.stop()

        self._join_process(self.acquisition_worker, "AcquisitionWorker")
        self._join_process(self.signal_processing_worker, "SignalProcessingWorker")

        self._close_queue(self.processor_command_queue)
        self._close_queue(self.acquisition_command_queue)

        logger.info("NeuroEngine: shutdown complete")

    # ------------------------------------------------------------------
    # Event routing — translate EventBus events into process commands
    #
    # The routing table is the single source of truth for "incoming EventType →
    # (target command queue, command to enqueue)".  Adding a route is one line;
    # there are no per-event handler methods to keep in sync.  Events needing
    # custom behaviour (not a plain queue.put) are registered explicitly below.
    # ------------------------------------------------------------------

    def _command_routes(self) -> dict:
        """EventType → (command queue, command) for simple put-command events."""
        acq = self.acquisition_command_queue
        proc = self.processor_command_queue
        return {
            EventType.ConnectDataProvider:            (acq, AcquisitionCommand.connect),
            EventType.DisconnectDataProvider:         (acq, AcquisitionCommand.disconnect),
            EventType.StartStream:                    (acq, AcquisitionCommand.start_stream),
            EventType.StopStream:                     (acq, AcquisitionCommand.stop_stream),
            EventType.StopNeuroEngineSignalProcessor: (proc, SignalProcessorCommand.should_stop),
        }

    def _register_event_listeners(self) -> None:
        for event_type, (queue, command) in self._command_routes().items():
            self.event_bus.register_event_listener(
                event_type, self._make_command_router(queue, command)
            )
        # Non-routing event: flips the shutdown flag, no command enqueued.
        self.event_bus.register_event_listener(
            EventType.CloseAllNeuroEngineProcessSystem, self._stop_neuro_engine_system
        )

    @staticmethod
    def _make_command_router(queue: mp.Queue, command) -> "callable":
        """Build a listener that enqueues *command* on *queue* (ignores the event)."""
        def _route(*_) -> None:
            queue.put(command)
        return _route

    def _stop_neuro_engine_system(self, *_) -> None:
        self.should_stop = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _join_process(process: mp.Process, name: str, timeout: int = 3) -> None:
        process.join(timeout=timeout)
        if process.is_alive():
            logger.warning("%s did not exit gracefully; terminating", name)
            process.terminate()

    @staticmethod
    def _close_queue(queue: mp.Queue) -> None:
        try:
            queue.close()
            queue.join_thread()
        except Exception as exc:
            logger.warning("Error closing queue: %s", exc)
