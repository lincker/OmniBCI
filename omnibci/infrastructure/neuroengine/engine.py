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
from omnibci.infrastructure.neuroengine.gateway import PeerGateway
from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler
from omnibci.infrastructure.neuroengine.effector_translator import EffectorCommandTranslator
from omnibci.infrastructure.neuroengine.effector_feedback import EffectorFeedbackHandler

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
        Routes EventBus events to/from external peers (stimulation, effector).
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
        effector_translator: "EffectorCommandTranslator | None" = None,
        effector_feedback: "EffectorFeedbackHandler | None" = None,
        debug: bool = False,
    ) -> None:
        self.event_bus = event_bus
        self.data_manager = data_manager
        self.acquisition_worker = acquisition_worker
        self.acquisition_command_queue = acquisition_command_queue
        self.signal_processing_worker = signal_processing_worker
        self.processor_command_queue = processor_command_queue
        self.peer_gateway = peer_gateway
        # Engine-side consumer of stimulation → engine feedback (StimulationAck).
        self.stim_feedback = StimulationFeedbackHandler()
        # Effector ACL + feedback — present only when the effector peer is
        # enabled (builder injects them; None disables all effector wiring).
        self.effector_translator = effector_translator
        self.effector_feedback = effector_feedback
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
        self.peer_gateway.start()
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
        await self.peer_gateway.stop()

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
        """
        EventType → (command queue, command, carry_data).

        carry_data=False: enqueue the bare command (legacy behaviour).
        carry_data=True:  enqueue ``(command, event.data)`` so the payload (e.g.
        a session_id) reaches the subprocess — see docs/session-id-unification.md.
        """
        acq = self.acquisition_command_queue
        proc = self.processor_command_queue
        return {
            EventType.ConnectDataProvider:            (acq, AcquisitionCommand.connect, False),
            EventType.DisconnectDataProvider:         (acq, AcquisitionCommand.disconnect, False),
            EventType.StartStream:                    (acq, AcquisitionCommand.start_stream, False),
            EventType.StopStream:                     (acq, AcquisitionCommand.stop_stream, False),
            EventType.StopNeuroEngineSignalProcessor: (proc, SignalProcessorCommand.should_stop, False),
            # Session lifecycle: REST-created session_id pushed into the worker.
            EventType.StartSession:                   (proc, SignalProcessorCommand.start_session, True),
            EventType.EndSession:                     (proc, SignalProcessorCommand.end_session, True),
        }

    def _register_event_listeners(self) -> None:
        for event_type, (queue, command, carry_data) in self._command_routes().items():
            self.event_bus.register_event_listener(
                event_type, self._make_command_router(queue, command, carry_data)
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
        # Effector wiring (only when enabled): translate each decoded trial into
        # an EffectorCommand, and consume the device's inbound feedback.
        if self.effector_translator is not None:
            self.event_bus.register_event_listener(
                EventType.TrialCompleted, self.effector_translator.on_trial_completed
            )
        if self.effector_feedback is not None:
            for event_type in (
                EventType.EffectorAck,
                EventType.EffectorState,
                EventType.EffectorFault,
            ):
                self.event_bus.register_event_listener(
                    event_type, self.effector_feedback.on_feedback
                )

    @staticmethod
    def _make_command_router(queue: mp.Queue, command, carry_data: bool = False) -> "callable":
        """
        Build a listener that enqueues *command* on *queue*.

        With carry_data the event's payload travels along as ``(command, data)``;
        otherwise the bare command is enqueued and the event is ignored.
        """
        if carry_data:
            def _route(event) -> None:
                queue.put((command, event.data))
        else:
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
