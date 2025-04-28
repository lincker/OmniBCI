#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-12-11 10:34

import time
import asyncio
from typing import List, Any
import numpy as np
from omnibci.core.data_model import DataPacket
from omnibci.core.analysis_method import AnalysisMethod
from omnibci.core.data_manager import DataManager
from .epoch import EpochManager
# check EEG data and detect the event


class IdleProcess:
    def __init__(self, analysis_controller, data_manager: DataManager, epoch_manager: EpochManager):
    # def __init__(self, data_queue, analysis_method: AnalysisMethod):
        self.data_manager = data_manager
        self.epoch_manager = epoch_manager
        self.controller = analysis_controller

    async def run(self):
        # TODO: get the data from the queue, refactor to a function

        if self.data_manager.empty():
            await asyncio.sleep(0.02)
            return

        data_packet: DataPacket = self.data_manager.get_packet()
        # detect whether the epoch begin
        is_head = self.epoch_manager.head_recognition(data_packet)
        if is_head:
            self.epoch_manager.set_epoch_head_data(data_packet)
            self.controller.current_status = self.controller.recognize


