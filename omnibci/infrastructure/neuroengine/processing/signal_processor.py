"""
SignalProcessor — 信号流处理状态机上下文。

消费 chunk 流，驱动整个识别循环：等待 epoch 开始 → 收集 epoch → 跑解码 →
在 BCISession 上记录 trial 并发布领域事件。生命周期跨 epoch 之前/之中/之后，
故命名落在"流"这一层，而非单个 epoch。

ACL（循环依赖修复）：
  - SignalProcessor 在 __init__ 中构造 DomainEventPublisher 并暴露给 states，
    state 模块不再 import 本模块，循环依赖消除。

进程包装 SignalProcessingWorker 已拆到 worker.py。
"""
from __future__ import annotations

import asyncio
from enum import Enum
import logging
from typing import Optional

from omnibci.domain.session import BCISession
from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.infrastructure.event_translator import DomainEventPublisher
from .processing_state import AwaitingEpochState, AssemblingEpochState
from .epoch.epoch_processor import EpochProcessor
from .epoch.pretrigger_buffer import PreTriggerBuffer

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'


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
        # controls session lifecycle; fall back to an auto-started session
        # if none is provided (e.g. in tests).
        if session is None:
            session = BCISession()
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

    def _execute(self, command: SignalProcessorCommand) -> None:
        if command == SignalProcessorCommand.should_stop:
            self._should_stop = True
            # End the session cleanly so SessionEnded domain event is emitted
            try:
                self.session.end()
            except RuntimeError:
                pass  # session may already be ended or never started
            self.event_publisher.publish_all(self.session.pop_events())
