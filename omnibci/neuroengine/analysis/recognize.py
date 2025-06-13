#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-11-09 22:51
import logging
import time
import asyncio
import math
import numpy as np
from scipy import signal
from omnibci.core.data_model import DataPacket
from omnibci.core.data_manager import DataManager
from .epoch import EpochManager
from omnibci.core.event.base import Event, EventType


class RecognizeProcess:
    def __init__(self, analysis_controller, data_manager: DataManager, epoch_manager: EpochManager, event_manager):
        self.controller = analysis_controller
        self.data_manager = data_manager
        self.epoch_manager = epoch_manager
        self.event_manager = event_manager

    async def run(self):
        if self.data_manager.empty():
            await asyncio.sleep(0.05)
            return

        data_packet: DataPacket = self.data_manager.get_packet()
        is_new_trial = self.epoch_manager.begin_recognition(data_packet)

        if is_new_trial:
            self.epoch_manager.append_epoch_tail_data(data_packet)
            _, result = self.epoch_manager.analysis(force_calc=True)
            logging.info(f"Recognized, result={result}")
            self.controller.current_status = self.controller.recognize
            self.epoch_manager.set_epoch_head_data(data_packet)
            event = Event(EventType.FeedbackResult, result)
            self.event_manager.put_event(event)
            return
        else:
            self.epoch_manager.append_data(data_packet)
            is_epoch_end, result = self.epoch_manager.analysis()
            if is_epoch_end:
                print(f"----------------- recognized: {result} -------------------")
                event = Event(EventType.FeedbackResult, result)
                self.event_manager.put_event(event)
                self.controller.current_status = self.controller.idle



