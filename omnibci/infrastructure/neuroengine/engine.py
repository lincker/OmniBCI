
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
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

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
        # Actuation ACL + feedback — present only when the actuation peer is
        # enabled (builder injects them; None disables all actuation wiring).
        self.actuation_translator = actuation_translator
        self.actuation_feedback = actuation_feedback
        self._debug = debug
        # P2-7：停机改为事件驱动。should_stop 保持公开属性语义（服务层与
        # 总线监听器都直接赋值），setter 负责跨线程唤醒主循环。
        self._should_stop = False
        self._stop_event: "asyncio.Event | None" = None   # serve() 内创建（绑定其 loop）
        self._loop: "asyncio.AbstractEventLoop | None" = None

    # ------------------------------------------------------------------
    # Stop flag — 事件驱动（P2-7）
    # ------------------------------------------------------------------

    @property
    def should_stop(self) -> bool:
        return self._should_stop

    @should_stop.setter
    def should_stop(self, value: bool) -> None:
        """置 True 时立即唤醒主循环（原实现靠 50ms 轮询发现标志位）。

        可能从任意线程赋值（NeuroEngineService 在 API 线程、
        _stop_neuro_engine_system 在引擎自身 loop 内），因此经
        call_soon_threadsafe 派发 Event.set；loop 尚未运行时只置标志——
        _main_loop 在等待前会先检查标志位。
        """
        self._should_stop = bool(value)
        if value and self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass  # loop 已关闭：serve 已经退出，无需唤醒

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking entry point — call from a dedicated thread."""
        asyncio.run(self.serve(), debug=self._debug)

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
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
        # 事件驱动等待（P2-7）：停机延迟从最多 50ms 降到 ≈0，空转唤醒
        # 从 20 次/秒降到 1 次/秒（超时仅作为跨线程写标志的兜底心跳）。
        while not self.should_stop:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

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
