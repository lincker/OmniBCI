#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-24 09:31
import asyncio
from enum import Enum
import logging
import multiprocessing as mp
import os


from omnibci.interfaces.algorithm import DecoderBase, EpochDetectorBase

from .state import WaitingForEpochState, CollectingEpochState
from .epoch import EpochManager

logger = logging.getLogger(__name__)


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'


class SignalProcessor:
    def __init__(self, detector:EpochDetectorBase, decoder:DecoderBase, command_queue, data_manager, event_bus):
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        self.epoch_manager = EpochManager(decoder=decoder, detector=detector)
        # Initialize states
        self._waiting_state = WaitingForEpochState(self)
        self._collecting_state = CollectingEpochState(self)
        self._current_state = self._waiting_state

        self._should_stop = False

    def run(self):
        asyncio.run(self.serve())

    def transition_to_waiting(self):
        self.epoch_manager.clear()
        self._current_state = self._waiting_state
        logger.debug("Transitioned to waiting for epoch state")

    def transition_to_collecting(self):
        self._current_state = self._collecting_state
        logger.debug("Transitioned to collecting epoch state")

    async def serve(self):
        while not self._should_stop:
            if not self.command_queue.empty():
                self.execute(self.command_queue.get())
            await self._current_state.run()

    def execute(self, command):
        if command == SignalProcessorCommand.should_stop:
            self._should_stop = True
            return


class SignalProcessorProcess(mp.Process):
    def __init__(self, command_queue, data_manager=None, event_bus=None):
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        self.signal_processor = None

    def run(self):
        algorithm, strategy = self._load_algorithm()
        self._run_processor(algorithm, strategy)

    def _load_algorithm(self):
        """Load algorithms plugins."""
        algorithm_plugin_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'plugins', 'algorithms'
        )
        from omnibci.core.utils.loader import PluginLoader
        from omnibci.core.factory import ObjectFactory
        PluginLoader.load_from_dir(algorithm_plugin_path, base_package='omnibci.plugins.algorithms')
        decoder = ObjectFactory.create(DecoderBase, config_key='ALGORITHM.decoder')
        detector = ObjectFactory.create(EpochDetectorBase, config_key='ALGORITHM.epoch_detector')

        return decoder, detector

    def _run_processor(self, decoder, detector):
        """Initialize and run the stream processor."""
        self.signal_processor = SignalProcessor(
            decoder=decoder,
            detector=detector,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus
        )
        self.signal_processor.run()

