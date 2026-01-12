#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-11-09 22:51
import asyncio
import logging

from omnibci.core.data_manager import DataManager
from omnibci.core.data_model import BCIStreamFrame
from omnibci.core.event.base import Event, EventType

from .epoch import EpochManager


class RecognizeState:
    def __init__(self, analysis_controller, data_manager: DataManager, epoch_manager: EpochManager, event_manager):
        self.controller = analysis_controller
        self.data_manager = data_manager
        self.epoch_manager = epoch_manager
        self.event_manager = event_manager

    async def run(self):
        if self.data_manager.empty():
            await asyncio.sleep(0.05)
            return

        data_packet: BCIStreamFrame = self.data_manager.get()

        # epoch end
        is_epoch_end = self.epoch_manager.end_recognition(data_packet)
        # new trial comes?
        is_new_trial_begin = self.epoch_manager.begin_recognition(data_packet)
        if not is_epoch_end and not is_new_trial_begin:
            # append data and calculate
            self.epoch_manager.append_data(data_packet)
            is_analysis_finished, result = self.epoch_manager.analysis()
            if is_analysis_finished:
                print(f"----------------- recognized: {result} -------------------")
                event = Event(EventType.FeedbackResult, result)
                self.event_manager.put_event(event)
                self.controller.current_status = self.controller.idle
            return

        if is_epoch_end:
            # force predict due to the trial data is end.
            self.epoch_manager.append_epoch_tail_data(data_packet)
            _, result = self.epoch_manager.analysis(force_predict=True)
            logging.info(f"Recognized, result={result}")
            self.controller.current_status = self.controller.idle
            event = Event(EventType.FeedbackResult, result)
            self.event_manager.put_event(event)


        if is_new_trial_begin:
            logging.info("new trial begin event detected")
            self.controller.current_status = self.controller.recognize
            if is_epoch_end:
                self.epoch_manager.set_epoch_head_data(data_packet)
                return
            else:
                self.epoch_manager.append_data_before_begin_event(data_packet)
                _, result = self.epoch_manager.analysis(force_predict=True)
                logging.info(f"new trial begin, force recognized, result={result}")
                self.controller.current_status = self.controller.recognize
                self.epoch_manager.set_epoch_head_data(data_packet)
                event = Event(EventType.FeedbackResult, result)
                self.event_manager.put_event(event)
                return




