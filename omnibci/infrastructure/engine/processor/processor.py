"""
SignalProcessor & SignalProcessingWorker — refactored.

Changes from original:
  - SignalProcessor now receives a BCISession and passes it to states via
    self.session, so CollectingEpochState can call session.record_trial().
  - EpochManager is constructed with sample_rate from config so Epoch VOs
    carry the correct metadata.
  - SignalProcessingWorker creates a BCISession (started) before building
    the SignalProcessor, and ends the session on clean shutdown.

ACL change (循环依赖修复):
  - _publish_domain_event 自由函数已移除，替换为注入的 DomainEventPublisher。
  - SignalProcessor 在 __init__ 中构造 event_publisher 并暴露给 states，
    state.py 不再需要延迟 import processor 模块，循环依赖消除。
"""
from __future__ import annotations

import asyncio
from enum import Enum
import logging
import multiprocessing as mp
import os
from typing import Optional

import omnibci
from omnibci.domain.session import BCISession
from omnibci.domain.ports.algorithm import DecoderBase, EpochDetectorBase
from omnibci.infrastructure.event_translator import DomainEventPublisher
from .state import WaitingForEpochState, CollectingEpochState
from .epoch_manager import EpochManager

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'


class SignalProcessor:
    """
    Orchestrates the epoch-detection state machine.

    Owns:
      - EpochManager  — buffer + decoder wrapper
      - BCISession    — aggregate root for trial recording
      - State machine — WaitingForEpochState / CollectingEpochState
    """

    def __init__(
        self,
        detector: EpochDetectorBase,
        decoder: DecoderBase,
        command_queue,
        data_manager,
        event_bus,
        session: Optional[BCISession] = None,
        sample_rate: int = 1000,
    ) -> None:
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # ACL: единственное место, где создаётся переводчик доменных событий.
        # States получают доступ через self.context.event_publisher — больше
        # никакого импорта processor.py из state.py.
        self.event_publisher = DomainEventPublisher(event_bus)

        self.epoch_manager = EpochManager(
            decoder=decoder,
            detector=detector,
            sample_rate=sample_rate,
        )

        # BCISession: created externally and injected so the process owner
        # controls session lifecycle; fall back to an auto-started session
        # if none is provided (e.g. in tests).
        if session is None:
            session = BCISession()
            session.start()
        self.session: BCISession = session

        # States hold a reference to this processor (the context)
        self._waiting_state = WaitingForEpochState(self)
        self._collecting_state = CollectingEpochState(self)
        self._current_state = self._waiting_state

        self._should_stop = False

    def run(self) -> None:
        asyncio.run(self.serve())

    def transition_to_waiting(self) -> None:
        self.epoch_manager.clear()
        self._current_state = self._waiting_state
        logger.debug("→ WaitingForEpochState")

    def transition_to_collecting(self) -> None:
        self._current_state = self._collecting_state
        logger.debug("→ CollectingEpochState")

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


class SignalProcessingWorker(mp.Process):
    """
    Multiprocessing wrapper for SignalProcessor.

    Creates a BCISession with a fresh ID and passes it into SignalProcessor
    so the session lifecycle is tied to this process.
    """

    def __init__(self, command_queue, data_manager=None, event_bus=None,
                 sample_rate: int = 1000) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # Injected in the parent process; pickled into the child on .start().
        self.sample_rate = sample_rate
        self.signal_processor: Optional[SignalProcessor] = None

    def run(self) -> None:
        decoder, detector = self._load_algorithm()
        self._run_processor(decoder, detector, self.sample_rate)

    def _load_algorithm(self):
        algorithm_plugin_path = os.path.join(
            next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'algorithms'
        )
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory

        PluginLoader.load_from_dir(
            algorithm_plugin_path,
            base_package='omnibci.infrastructure.plugins.algorithms',
        )
        decoder = ObjectFactory.create(DecoderBase, config_key='ALGORITHM.decoder')
        detector = ObjectFactory.create(EpochDetectorBase, config_key='ALGORITHM.epoch_detector')

        # sample_rate is injected at construction (self.sample_rate), no settings read here.
        return decoder, detector

    def _run_processor(self, decoder, detector, sample_rate: int) -> None:
        session = BCISession()
        session.start()
        logger.info("BCISession started: %s", session.session_id)

        self.signal_processor = SignalProcessor(
            decoder=decoder,
            detector=detector,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            session=session,
            sample_rate=sample_rate,
        )
        self.signal_processor.run()
