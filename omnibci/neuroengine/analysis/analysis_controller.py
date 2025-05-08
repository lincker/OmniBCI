#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-10-20 14:28
from enum import Enum

import asyncio

from .classifiers.cca_sklearn import CCA
from .idle import IdleProcess
from .recognize import RecognizeProcess
from .epoch import EpochManager


# check the current state, if the state is wait, then run the wait process, else run the detect process
# in the detect process, check the data_queue, if there is data, then get one package data and run, if empty sleep 0.1s
# if the detect process detect the event, then run the event process, else run the wait process


class AnalysisCommand(Enum):
    should_stop = 'analysis_controller_stop'


class AnalysisController:
    def __init__(self, command_queue, data_manager, event_manager):
        self.data_manager = data_manager
        self.analysis_method = CCA()
        self.event_manager = event_manager
        self.epoch_manager = EpochManager(analysis_method=self.analysis_method)
        self.idle = IdleProcess(self, self.data_manager, self.epoch_manager)
        self.recognize = RecognizeProcess(self, self.data_manager, self.epoch_manager, self.event_manager)
        self.current_status = self.idle
        self.command_queue = command_queue
        self.should_stopped = False

    def run(self):
        asyncio.run(self.serve())

    async def serve(self):
        while not self.should_stopped:
            if not self.command_queue.empty():
                self.execute(self.command_queue.get())
            await self.current_status.run()
            # await asyncio.sleep(0.01)

    def execute(self, command):
        if command == AnalysisCommand.should_stop:
            self.should_stopped = True
            return



