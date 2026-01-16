#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-24 09:31
import asyncio
from enum import Enum
import logging
import multiprocessing as mp
import os


from omnibci.core.base.algorithm import AlgorithmBase, EpochStrategyBase

from .state import WaitingForEpochState, CollectingEpochState
from .epoch import EpochManager


class SignalProcessorCommand(Enum):
    should_stop = 'stream_processor_stop'


class SignalProcessor:
    def __init__(self, strategy:EpochStrategyBase, algorithm:AlgorithmBase, command_queue, data_manager, event_manager):
        self.data_manager = data_manager
        self.event_manager = event_manager
        self.command_queue = command_queue
        self.epoch_manager = EpochManager(algorithm=algorithm, strategy=strategy)
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
        logging.debug("Transitioned to waiting for epoch state")

    def transition_to_collecting(self):
        self._current_state = self._collecting_state
        logging.debug("Transitioned to collecting epoch state")

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
    def __init__(self, command_queue, data_manager=None, event_manager=None):
        super().__init__()
        self.data_manager = data_manager
        self.event_manager = event_manager
        self.command_queue = command_queue
        self.stream_processor = None

    def run(self):
        algorithm, strategy = self._load_plugins()
        self._run_processor(algorithm, strategy)

    def _load_plugins(self):
        """Load algorithm plugins."""
        from omnibci.core.base.factory import load_plugins, AlgorithmFactory, EpochStrategyFactory

        algorithm_plugin_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'plugin', 'algorithm'
        )
        load_plugins(algorithm_plugin_path)

        logging.info("Loading algorithm plugins in subprocess")
        algorithm = AlgorithmFactory.get_algorithm()
        strategy = EpochStrategyFactory.get_strategy()

        return algorithm, strategy

    def _run_processor(self, algorithm, strategy):
        """Initialize and run the stream processor."""
        self.stream_processor = SignalProcessor(
            strategy=strategy,
            algorithm=algorithm,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_manager=self.event_manager
        )
        self.stream_processor.run()

